from __future__ import annotations

import json
import os
import secrets
import re
import time
from pathlib import Path
from threading import Lock, RLock
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, render_template, request, session, stream_with_context, send_from_directory
from http_transport import RequestException

from lingxi_client import Account, LingxiError, chat, exchange_cookie, verify_token
from app_login import AppLoginError, AppLoginSession, AppCredentialError
from unified_logins import UnifiedLogins
from login_storage import DatabaseLoginStorage, LoginStorageError
from session_maintenance import SessionMaintenance
from recharge.cmcc import ActivityError
from lingxi_scheduler import LingxiScheduler, TaskError
from checkin_client import CheckinError, CheckinClient
from checkin_scheduler import CheckinScheduler, CheckinTaskError
from monthly_coupon_auth import MonthlyCouponAuthError
from monthly_coupon_client import MonthlyCouponClient
from monthly_coupon_scheduler import MonthlyCouponScheduler, MonthlyTaskError
from star_day_client import StarDayError
from star_day_auth import StarDayAuthError
from star_day_scheduler import StarDayScheduler
from credential_cache import CredentialCache
from platform_manager import PlatformManager
import activities
from coupon_client import CouponError, SORTS as COUPON_SORTS
from coupon_use import CouponUseManager
from coupon_captcha import register_coupon_captcha
from runtime_paths import DATA_DIR,RECHARGE_DATA_DIR
from access_security import AccessGate, register_access
from update_checker import register_updates
from network_access import register_network


def create_app(*, enable_recharge=False, recharge_manager=None, recharge_scheduler=None, database_path=None, recharge_data_dir=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secrets.token_bytes(32),
        MAX_CONTENT_LENGTH=64 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
    )
    register_coupon_captcha(app)
    accounts: dict[str, Account] = {}
    accounts_lock = RLock()
    chat_states: dict[tuple[str, str], Account] = {}
    management_locks = {}
    lingxi_auth_locks = {}
    login_flows: dict[str, AppLoginSession] = {}
    database = DatabaseLoginStorage(database_path) if database_path is not None else None
    access_security = register_access(app, database)
    register_network(app, database)
    register_updates(app, database)
    credential_cache = CredentialCache(database)
    app.extensions['credential_cache'] = credential_cache
    unified = UnifiedLogins(database)
    app.extensions["lingxi_accounts"] = accounts
    app.extensions['unified_logins'] = unified
    app.extensions['lingxi_chat_states'] = chat_states

    def shared_credentials(login_id):
        # Business consumers never acquire a phone lock while holding their
        # operation lock. Rotation is committed atomically in the pool.
        with unified.lock:
            saved = unified.get_id(login_id)
            if saved is not None and saved.status == 'login_required':
                raise ActivityError('APP 会话需要重新验证，请打开账号管理','APP_LOGIN_REQUIRED',401)
            value = unified.credentials(login_id)
            return {**value,'id':login_id} if value is not None else None

    def all_chat_accounts():
        with accounts_lock:
            return list({id(a): a for a in [*accounts.values(), *chat_states.values()]}.values())

    def state_for(saved):
        account()
        key = (session['sid'], saved.id)
        with accounts_lock:
            if key not in chat_states:
                target = Account(auth_code=saved.token, verified=bool(saved.token), app_cookie=saved.cookie,
                                 app_phone=saved.phone, app_profile=dict(saved.profile))
                chat_states[key] = target
            target = chat_states[key]
            target.app_cookie, target.app_profile = saved.cookie, dict(saved.profile)
            target.auth_code, target.verified = saved.token, bool(saved.token)
            return target

    def apply_login(current, record):
        target = state_for(record)
        # Updating credentials never resets this phone's conversation session.
        target.auth_code, target.verified = record.token, True
        target.app_cookie, target.app_phone = record.cookie, record.phone
        target.app_profile = dict(record.profile)
        with accounts_lock:
            accounts[session['sid']] = target
        session['login_id'] = record.id
        return target

    def sync_chat_credentials(saved):
        for logged in all_chat_accounts():
            if logged.app_phone == saved.phone and not logged.lock.locked():
                logged.auth_code, logged.verified = saved.token, bool(saved.token)
                logged.app_cookie, logged.app_profile = saved.cookie, dict(saved.profile)

    def refresh_app_credentials(saved, *, force=False):
        with unified.phone_lock(saved.phone):
            if coupon_uses.busy_login(saved.id):
                return
            snapshot = unified.credentials(saved.id)
            if snapshot is None:
                raise AppCredentialError('账号已退出，请重新选择账号。',login_required=True)
            if not force and time.time() - snapshot['app_context'].get('refreshed_at',0) < 600:
                return
            revision = snapshot['credential_revision']
            flow = AppLoginSession.from_credentials(snapshot)
            try:
                flow.refresh()
            except AppLoginError as exc:
                required = isinstance(exc,AppCredentialError) and exc.login_required
                unified.mark(saved,'login_required' if required else 'active',str(exc),expected_revision=revision)
                raise
            updated = unified.rotate_credentials(saved.phone,expected_revision=revision,cookie=flow.cookie,
                        app_context=flow.export_context(),profile=flow.user_info)
            if updated is None:
                raise AppCredentialError('账号凭据已变更，请重试。')
            app.extensions['session_maintenance'].due.pop(saved.id,None)

    def ensure_token(saved, *, refresh=False):
        # Only APP renewal needs the shared phone lock. Slow Lingxi verification
        # uses its own lock so wallet/OTP operations remain independent.
        if refresh or saved.app_context.get('device_id'):
            refresh_app_credentials(saved,force=refresh)
        with accounts_lock:
            token_lock = lingxi_auth_locks.setdefault(saved.id,RLock())
        for attempt in range(2):
            retry = False
            with token_lock:
                snapshot = unified.credentials(saved.id)
                if snapshot is None:
                    raise AppCredentialError('账号已退出。',login_required=True)
                revision = snapshot['credential_revision']
                with unified.lock:
                    token = saved.token
                try:
                    if not token:
                        token = exchange_cookie(snapshot['cookie'])
                        verify_token(token)
                        verified_at = time.time()
                    elif refresh or time.time()-saved.app_context.get('lingxi_verified_at',0) >= 600:
                        try:
                            verify_token(token)
                        except LingxiError as exc:
                            if not exc.authentication:
                                raise
                            token = exchange_cookie(snapshot['cookie'])
                            verify_token(token)
                        verified_at = time.time()
                    else:
                        verified_at = saved.app_context.get('lingxi_verified_at',0)
                except LingxiError as exc:
                    if exc.authentication and attempt == 0 and not refresh:
                        retry = True
                    else:
                        unified.mark(saved,'active','灵犀会话将在使用时重连，其他功能可继续使用',expected_revision=revision)
                        raise
                if not retry:
                    with unified.lock:
                        if unified.get(saved.phone) is not saved:
                            raise AppCredentialError('账号已退出。',login_required=True)
                        if saved.credential_revision != revision:
                            continue
                        changed = saved.token != token
                        saved.token = token
                        changed = changed or saved.app_context.get('lingxi_verified_at') != verified_at
                        saved.app_context['lingxi_verified_at'] = verified_at
                        unified.mark(saved,'active','APP 会话已维护，业务使用最新凭据',expected_revision=revision)
                        if changed:
                            unified.save()
                    sync_chat_credentials(saved)
                    return
            if retry:
                refresh_app_credentials(saved,force=True)
        raise AppCredentialError('账号凭据更新中，请稍后重试。')

    def reuse_login(current, saved, activate=True):
        ensure_token(saved)
        return apply_login(current, saved) if activate else current

    def request_chat_account():
        data = request.get_json(silent=True) if request.is_json else {}
        login_id = request.args.get('login_id') or (data.get('login_id') if isinstance(data, dict) else None)
        if login_id:
            saved = unified.get_id(login_id)
            return state_for(saved) if saved else None
        return account()

    def login_operation_lock(current, manage):
        if not manage:
            return current.lock
        with accounts_lock:
            return management_locks.setdefault(session['sid'], Lock())

    def account():
        sid = session.get("sid")
        with accounts_lock:
            if sid not in accounts:
                sid = secrets.token_hex(24)
                session["sid"] = sid
                accounts[sid] = Account()
            return accounts[sid]

    @app.before_request
    def local_network_access():
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                return jsonify(error="请求来源不正确。"), 403
            if request.path.startswith('/coupon-verification/'):
                return None  # The fixed verification gateway validates its SDK form bodies.
            if not request.is_json:
                return jsonify(error="请使用 JSON 请求。"), 415
            if not isinstance(request.get_json(silent=True), dict):
                return jsonify(error="请使用 JSON 对象。"), 400

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        )
        if request.path == '/static/coupon-captcha.html':
            response.headers['Content-Security-Policy'] = (
                "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; img-src 'self' data: blob:; frame-ancestors 'self'"
            )
        return response

    @app.get("/")
    def index():
        account()
        return render_template("home.html")

    @app.get('/favicon.ico')
    def favicon():return send_from_directory(app.static_folder,'app.ico',mimetype='image/x-icon')

    @app.get('/accounts')
    def account_center():
        account()
        return render_template('accounts.html')

    @app.get('/lingxi')
    def lingxi_page():
        account()
        return render_template('index.html')

    @app.get('/lingxi/tasks')
    def lingxi_tasks_page():
        return render_template('lingxi_tasks.html')

    @app.get('/checkin')
    def checkin_page():
        return render_template('checkin.html')

    @app.get('/monthly-coupon')
    def monthly_coupon_page():
        return render_template('monthly_coupon.html')

    @app.errorhandler(MonthlyTaskError)
    @app.errorhandler(MonthlyCouponAuthError)
    def monthly_coupon_error(error):
        return jsonify(error=str(error)), error.status

    @app.get('/api/monthly-coupon/status')
    def monthly_coupon_status():
        return jsonify(monthly_scheduler.status())

    @app.post('/api/monthly-coupon/run')
    def run_monthly_coupon():
        return jsonify(message=monthly_scheduler.run(request.get_json().get('login_ids')))

    @app.post('/api/monthly-coupon/settings')
    def save_monthly_coupon_settings():
        return jsonify(message=monthly_scheduler.settings(request.get_json()))

    @app.post('/api/monthly-coupon/page')
    def save_monthly_coupon_page():
        return jsonify(monthly_scheduler.update_page(request.get_json()))

    @app.errorhandler(CheckinTaskError)
    def checkin_task_error(error):
        return jsonify(error=str(error)), error.status

    @app.get('/coupons')
    def coupons_page():
        return render_template('coupons.html')

    @app.errorhandler(CouponError)
    def coupon_error(error):
        return jsonify(error=str(error), code=error.code), error.status

    @app.get('/api/coupons/accounts')
    def coupon_accounts():
        return jsonify(accounts=unified.public())

    def coupon_credentials(login_id):
        with unified.lock:
            saved = unified.get_id(login_id)
            if saved is None:
                raise CouponError('请选择共享账号池中的账号。', 'ACCOUNT_NOT_FOUND', 404)
            if saved.status != 'active':
                raise CouponError('此账号尚未就绪，请从左侧账号管理恢复登录。', 'APP_LOGIN_REQUIRED', 401)
            return {**unified.credentials(login_id),'id':login_id}

    # This wallet entry was supplied by the user (dx.10086.cn/A/aIU7Gw).
    # Its public deep-link channel applies to operations entered through this URL,
    # independently of the account's private APP login cookie.
    coupon_uses = CouponUseManager(coupon_credentials, storage=database,
        payment_credentials_provider_by_id=unified.credentials,
        wallet_entry='https://touch.10086.cn/i/mobile/mycoupons.html?channelId=P00000004912',credential_cache=credential_cache)
    app.extensions['coupon_uses'] = coupon_uses

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
        if state not in ('available', 'history') or sort not in COUPON_SORTS or not re.fullmatch(r'[1-9]\d{0,3}', page):
            raise CouponError('卡券筛选或页码无效，请刷新后重试。', 'INVALID_QUERY', 400)
        saved = unified.get_id(login_id)
        if saved is None:
            raise CouponError('请选择共享账号池中的账号。', 'ACCOUNT_NOT_FOUND', 404)
        with unified.phone_lock(saved.phone):
            result = coupon_uses.query(login_id, state=state, page=int(page), sort=sort)
        coupon_uses.bind_pending(login_id, result['items'])
        result['pending_orders'] = coupon_uses.pending(login_id)
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

    @app.errorhandler(LoginStorageError)
    def account_database_error(error):
        return jsonify(error=str(error)), 503

    @app.get('/api/checkin/status')
    def checkin_status():
        return jsonify(checkin_scheduler.status())

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
        return jsonify(products=star_scheduler.products())

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

    @app.post('/api/checkin/settings')
    def save_checkin_settings():
        return jsonify(checkin_scheduler.update_settings(request.get_json()))

    @app.post('/api/checkin/tasks/save')
    def save_checkin_tasks():
        return jsonify(message=checkin_scheduler.save(request.get_json()))

    @app.post('/api/checkin/tasks/stop')
    def stop_checkin_tasks():
        checkin_scheduler.stop(request.get_json().get('ids'))
        return jsonify(message='已停止所选任务，已发出的请求会保留结果')

    @app.post('/api/checkin/run')
    def run_checkin():
        data = request.get_json()
        if data.get('kind') != 'signin':
            return jsonify(error='此入口仅执行每日签到。'), 400
        return jsonify(message=checkin_scheduler.run(data.get('login_ids')))

    @app.errorhandler(TaskError)
    def lingxi_task_error(error):
        return jsonify(error=str(error)),error.status

    @app.get('/api/lingxi/tasks')
    def lingxi_tasks():
        return jsonify(tasks=lingxi_scheduler.snapshots(),warning=lingxi_scheduler.warning,
                       server_time=lingxi_scheduler.now().isoformat(),settings=lingxi_scheduler.settings())

    @app.get('/api/lingxi/settings')
    def lingxi_settings():
        return jsonify(settings=lingxi_scheduler.settings())

    @app.post('/api/lingxi/settings')
    def save_lingxi_settings():
        return jsonify(settings=lingxi_scheduler.update_settings(request.get_json()),message='发送设置已保存并立即生效')

    @app.get('/api/lingxi/tasks/<task_id>')
    def lingxi_task_detail(task_id):
        tasks=lingxi_scheduler.snapshots(task_id)
        if not tasks:raise TaskError('任务不存在。',404)
        return jsonify(task=tasks[0])

    @app.post('/api/lingxi/tasks/save')
    def save_lingxi_task():
        ids=lingxi_scheduler.save(request.get_json())
        return jsonify(ids=ids,message='已保存灵犀定时发送任务')

    @app.post('/api/lingxi/tasks/stop')
    def stop_lingxi_task():
        lingxi_scheduler.stop(request.get_json().get('id'))
        return jsonify(message='已停止后续发送，已发出的请求继续保留回复')

    @app.post('/api/lingxi/tasks/stop-batch')
    def stop_lingxi_tasks():
        ids=lingxi_scheduler.stop_batch(request.get_json())
        return jsonify(ids=ids,message=f'已停止 {len(ids)} 项任务；正在接收的回复会保留，结束后可删除')

    @app.post('/api/lingxi/tasks/adjust')
    def adjust_lingxi_tasks():
        ids=lingxi_scheduler.adjust_batch(request.get_json())
        return jsonify(ids=ids,message=f'已调整 {len(ids)} 项任务')

    @app.post('/api/lingxi/tasks/delete')
    def delete_lingxi_task():
        lingxi_scheduler.delete(request.get_json().get('id'))
        return jsonify(message='任务已删除')

    @app.post('/api/lingxi/tasks/delete-batch')
    def delete_lingxi_tasks():
        ids=lingxi_scheduler.delete_batch(request.get_json())
        return jsonify(ids=ids,message=f'已删除 {len(ids)} 项任务及执行记录')

    @app.get("/api/status")
    def status():
        current = request_chat_account()
        return jsonify(**(current.status() if current else Account().status()))

    @app.get('/api/logins')
    def saved_logins():
        selected = unified.get(account().app_phone)
        return jsonify(accounts=unified.public(), active_id=selected.id if selected else '',
                       storage='sqlite' if database is not None else 'memory', warning=unified.warning)

    @app.post('/api/login/select')
    def select_login():
        data = request.get_json(silent=True) or {}
        saved = unified.get_id(data.get('id'))
        if saved is None:
            return jsonify(error='该账号登录态不存在，请重新短信登录。'), 400
        current = state_for(saved)
        held_lock = current.lock
        if not held_lock.acquire(blocking=False):
            return jsonify(error='这个账号正在接收回复，请稍后切换。'), 409
        try:
            with unified.phone_lock(saved.phone):
                current = reuse_login(current, saved)
            return jsonify(**current.status(), login_id=saved.id)
        except (LingxiError,AppLoginError) as exc:
            return jsonify(error=str(exc)), 400
        except RequestException:
            return jsonify(error='连接登录验证服务失败，请稍后重试。'), 502
        finally:
            held_lock.release()

    def login_flow():
        account()
        sid = session["sid"]
        with accounts_lock:
            if sid not in login_flows:
                login_flows[sid] = AppLoginSession()
            return login_flows[sid]

    @app.post("/api/logins/sms")
    @app.post("/api/login/sms")
    @app.post('/api/logins/relogin/sms')
    def send_login_sms():
        value = request.get_json(silent=True)
        relogin = request.path == '/api/logins/relogin/sms'
        target = unified.get_id(value.get('id')) if relogin else None
        if relogin and target is None:
            return jsonify(error='账号已退出，请重新添加账号。'), 404
        phone = target.phone if relogin else value.get('phone')
        if not relogin and (not isinstance(phone, str) or not re.fullmatch(r'1[3-9][0-9]{9}', phone)):
            return jsonify(error='请输入正确的 11 位手机号。'), 400
        manage = request.path.startswith('/api/logins/')
        current = account()
        held_lock = login_operation_lock(current, manage)
        if not held_lock.acquire(blocking=False):
            return jsonify(error="正在登录或接收回复，请稍候。"), 409
        try:
            with unified.phone_lock(phone):
                saved = unified.get(phone)
                if relogin and saved is not target:
                    return jsonify(error='账号已退出，请重新添加账号。'), 404
                if saved and saved.status != 'login_required' and not (relogin and value.get('force') is True):
                    try:
                        current = reuse_login(current, saved, activate=not manage)
                        return jsonify(reused=True, account=saved.public()) if manage else jsonify(reused=True, **current.status())
                    except LingxiError as exc:
                        raise
                    except AppCredentialError as exc:
                        if not exc.login_required:
                            raise
                with unified.lock:
                    flow = unified.pending.get(phone)
                if flow is not None and flow.phone == phone and time.monotonic() - flow.sms_sent_at < 600:
                    with accounts_lock:
                        login_flows[session['sid']] = flow
                    if relogin and value.get('resend') is True:
                        return jsonify(**flow.send_sms(phone))
                    return jsonify(sent=True, phone_masked=phone[:3]+'****'+phone[-4:],
                                   retry_after=max(0, int(60 - (time.monotonic() - flow.sms_sent_at))))
                flow = AppLoginSession()
                with accounts_lock:
                    login_flows[session['sid']] = flow
                result = flow.send_sms(phone)
                with unified.lock:
                    unified.pending[phone] = flow
                return jsonify(**result)
        except AppLoginError as exc:
            return jsonify(error=str(exc)), 400
        except LingxiError as exc:
            return jsonify(error=str(exc)), 400
        except RequestException:
            return jsonify(error='连接登录验证服务失败，请稍后重试。'), 502
        finally:
            held_lock.release()

    @app.post("/api/logins/login")
    @app.post("/api/login")
    @app.post('/api/logins/relogin')
    def login():
        value = request.get_json(silent=True)
        relogin = request.path == '/api/logins/relogin'
        target = unified.get_id(value.get('id')) if relogin else None
        if relogin and target is None:
            return jsonify(error='账号已退出，请重新添加账号。'), 404
        phone, code = target.phone if relogin else value.get('phone'), value.get('code')
        if not isinstance(phone, str) or not isinstance(code, str):
            return jsonify(error="请输入手机号和短信验证码。"), 400
        name = '' if relogin else value.get('name','')
        if not isinstance(name, str) or len(name.strip()) > 40:
            return jsonify(error="账号备注最多 40 个字。"), 400
        manage = request.path.startswith('/api/logins/')
        current = account()
        held_lock = login_operation_lock(current, manage)
        if not held_lock.acquire(blocking=False):
            return jsonify(error="正在登录或接收回复，请稍候。"), 409
        try:
            with unified.phone_lock(phone):
                saved = unified.get(phone)
                if relogin and saved is not target:
                    return jsonify(error='账号已退出，请重新添加账号。'), 404
                if saved and saved.status != 'login_required' and not (relogin and value.get('force') is True):
                    current = reuse_login(current, saved, activate=not manage)
                else:
                    with unified.lock:
                        pending = unified.pending.get(phone)
                    flow = pending if pending is not None else login_flow()
                    cookie = flow.login(phone, code)
                    token = exchange_cookie(cookie)
                    verify_token(token)
                    profile = dict(flow.user_info) if isinstance(flow.user_info, dict) else {}
                    context = flow.export_context()
                    saved = unified.remember(phone, cookie, profile, token, name.strip(),
                                             app_context=context if isinstance(context,dict) else None)
                    app.extensions['session_maintenance'].due.pop(saved.id, None)
                    if not manage:
                        current = apply_login(current, saved)
            with accounts_lock:
                login_flows.pop(session["sid"], None)
            return jsonify(account=saved.public(), message='账号已重新登录，任务绑定和备注已保留' if relogin else '账号已加入共享账号池') if manage else jsonify(**current.status(), login_id=saved.id)
        except (AppLoginError, LingxiError) as exc:
            return jsonify(error=str(exc)), 400
        except RequestException:
            return jsonify(error="连接灵犀登录服务失败，请稍后重试。"), 502
        finally:
            held_lock.release()

    @app.post('/api/logins/rename')
    def rename_login():
        data = request.get_json(silent=True) or {}
        saved = unified.get_id(data.get('id'))
        name = data.get('name', '')
        if saved is None or not isinstance(name, str) or len(name.strip()) > 40:
            return jsonify(error='请选择账号，备注最多 40 个字。'), 400
        with unified.lock:
            saved.name = name.strip()
            unified.save()
        return jsonify(account=saved.public())

    @app.post('/api/logins/check')
    def check_login():
        data = request.get_json(silent=True) or {}
        saved = unified.get_id(data.get('id'))
        if saved is None:
            return jsonify(error='账号已退出，请重新登录。'), 400
        try:
            with unified.phone_lock(saved.phone):
                ensure_token(saved,refresh=True)
                restore_activity_sessions(saved)
            return jsonify(account=saved.public(), message='登录态有效，关联活动会话已恢复')
        except (LingxiError,AppLoginError) as exc:
            return jsonify(error=str(exc)), 400
        except RequestException:
            return jsonify(error='连接验证服务失败，请稍后重试。'), 502
        except ActivityError as exc:
            return jsonify(error=str(exc)), exc.status

    def clear_login_states(phone):
        saved=unified.get(phone)
        if saved is not None:
            platforms.forget(saved.id)
            star_scheduler.forget(saved.id)
            monthly_scheduler.forget(saved.id)
            checkin_scheduler.forget(saved.id)
            lingxi_scheduler.cancel_login(saved.id)
            credential_cache.forget(saved.id)
        unified.forget(phone)
        for logged in all_chat_accounts():
            if logged.app_phone == phone:
                logged.clear()

    @app.post('/api/logins/logout')
    def logout_managed_login():
        data = request.get_json(silent=True) or {}
        saved = unified.get_id(data.get('id'))
        if saved is None:
            return jsonify(error='账号已退出。'), 400
        if enable_recharge:
            from recharge.cmcc import ActivityError
            try:
                forget_app_login(saved.phone)
            except ActivityError as exc:
                return jsonify(error=str(exc)), exc.status
        else:
            if platforms.busy_login(saved.id):
                return jsonify(error='该账号正在执行活动或短信验证，请完成后退出。'), 409
            if any(a.app_phone == saved.phone and a.lock.locked() for a in all_chat_accounts()):
                return jsonify(error='该账号正在接收回复，请稍后退出。'), 409
            clear_login_states(saved.phone)
        return jsonify(accounts=unified.public())

    @app.post("/api/logout")
    def logout():
        current = account()
        if current.lock.locked():
            return jsonify(error="请先停止接收当前回复。"), 409
        if current.app_phone:
            if enable_recharge:
                try:
                    forget_app_login(current.app_phone)
                except Exception as exc:
                    from recharge.cmcc import ActivityError
                    if isinstance(exc, ActivityError):
                        return jsonify(error=str(exc)), exc.status
                    raise
            else:
                clear_login_states(current.app_phone)
        current.clear()
        with accounts_lock:
            login_flows.pop(session["sid"], None)
        return jsonify(**current.status())

    @app.post("/api/conversation/new")
    def new_conversation():
        current = request_chat_account()
        if current is None:
            return jsonify(error="请先选择已登录账号。"), 401
        if current.lock.locked():
            return jsonify(error="请先停止接收当前回复。"), 409
        current.session_id = secrets.token_hex(16)
        return jsonify(ok=True)

    @app.post("/api/chat")
    def send_message():
        value = request.get_json(silent=True)
        message = value.get("message") if isinstance(value, dict) else None
        if not isinstance(message, str) or not message.strip() or len(message) > 8000:
            return jsonify(error="请输入 1 到 8000 个字符的文字。"), 400
        current = request_chat_account()
        if current is None or not current.auth_code and not unified.get(current.app_phone):
            return jsonify(error="请先输入手机号和验证码登录。"), 401
        saved = unified.get(current.app_phone) if current.app_phone else None
        if saved:
            try:
                with unified.phone_lock(saved.phone):
                    if saved.status != 'active' or time.time() - saved.checked_at > 300:
                        ensure_token(saved)
                    current.auth_code, current.verified = saved.token, True
                    current.app_cookie, current.app_profile = saved.cookie, dict(saved.profile)
            except AppLoginError as exc:
                return jsonify(error=str(exc)), 401 if isinstance(exc,AppCredentialError) and exc.login_required else 502
            except LingxiError as exc:
                return jsonify(error=str(exc)), 401 if exc.authentication else 502
            except RequestException:
                return jsonify(error='暂时无法验证会话，请稍后重试。'), 502

        def events():
            used_token = current.auth_code
            try:
                for item in chat(current, message.strip()):
                    yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
            except LingxiError as exc:
                if exc.authentication and saved:
                    with unified.lock:
                        if saved.token == used_token:
                            saved.token = ''
                            saved.app_context['lingxi_verified_at'] = 0
                            unified.save()
                    unified.mark(saved, 'active', '灵犀会话将在使用时重连，其他功能可继续使用')
                yield "data: " + json.dumps({"type": "error", "message": str(exc), "authentication": exc.authentication}, ensure_ascii=False) + "\n\n"
            except (RequestException, UnicodeError):
                yield 'data: {"type":"error","message":"网络连接中断，请重试。"}\n\n'

        return Response(stream_with_context(events()), mimetype="text/event-stream", headers={"X-Accel-Buffering": "no"})

    if enable_recharge:
        from werkzeug.middleware.dispatcher import DispatcherMiddleware
        from recharge.app import create_app as create_recharge_app
        from recharge.accounts import AccountManager
        from recharge.cmcc import ROOT as RECHARGE_ROOT

        def forget_app_login(phone):
            with unified.phone_lock(phone):
                forget_locked_app_login(phone)

        def forget_locked_app_login(phone):
            from recharge.cmcc import ActivityError
            saved = unified.get(phone)
            if saved is not None and platforms.busy_login(saved.id):
                raise ActivityError('该账号正在执行活动或短信验证，请完成后退出。', 'ACCOUNT_BUSY', 409)
            if saved is not None and star_scheduler.busy_login(saved.id):
                raise StarDayError('该账号正在执行星动日任务，请完成后退出。', 'ACCOUNT_BUSY', 409)
            if saved is not None and monthly_scheduler.busy_login(saved.id):
                raise MonthlyTaskError('该账号正在执行每月话费券，请完成后退出。', 409)
            if saved is not None and checkin_scheduler.busy_login(saved.id):
                raise CheckinTaskError('该账号正在签到或领取，请完成后退出。', 409)
            with accounts_lock:
                if any(a.app_phone == phone and a.lock.locked() for a in all_chat_accounts()):
                    # The caller's own logout holds no account lock.
                    raise ActivityError('该账号正在接收回复或登录，请完成后退出。', 'ACCOUNT_BUSY', 409)
            manager = recharge.extensions['accounts']
            scheduler = recharge.extensions['scheduler']
            saved = unified.get(phone)
            linked = [a for a in manager.accounts.values() if saved and a.pool_login_id == saved.id
                      or a.client.app_material and a.client.app_material.get('phone') == phone]
            if any(scheduler.busy(a.id) for a in linked):
                raise ActivityError('请先停止该账号的活动任务，再退出统一登录。', 'JOB_RUNNING', 409)
            held = []
            try:
                for linked_account in linked:
                    if not linked_account.client.operation_lock.acquire(blocking=False):
                        raise ActivityError('该账号会话正在恢复或查询，请完成后退出。', 'ACCOUNT_BUSY', 409)
                    held.append(linked_account.client.operation_lock)
                for linked_account in linked:
                    scheduler.cancel([linked_account.id])
                    linked_account.client.logout()
                    linked_account.pool_login_id = ''
                    manager.update(linked_account, {'authenticated': False, 'mode': 'none', 'phone': '', 'session_status':'login_required'})
                clear_login_states(phone)
            finally:
                for lock in reversed(held):
                    lock.release()

        def current_app_login(login_id=None):
            if login_id:
                saved = unified.get_id(login_id)
                value = unified.credentials(saved.id) if saved and saved.status != 'login_required' else None
                return {key:value[key] for key in ('cookie','phone','profile')} if value else None
            # The mounted app has a distinct CSRF session. Resolve only this
            # browser's signed Lingxi session; never select another browser's account.
            browser_session = app.session_interface.open_session(app, request)
            if browser_session is None:
                return None
            with accounts_lock:
                logged = accounts.get(browser_session.get('sid'))
                if not logged:
                    return None
                saved = unified.get(logged.app_phone)
            return current_app_login(saved.id) if saved else None

        if recharge_manager is None and (database is not None or recharge_data_dir is not None):
            activity_data=Path(recharge_data_dir) if recharge_data_dir is not None else RECHARGE_ROOT/'.local'
            recharge_manager = AccountManager(storage=activity_data/'accounts.json', database=database)
        if recharge_scheduler is None and recharge_data_dir is not None:
            from recharge.scheduler import ClaimScheduler
            recharge_scheduler=ClaimScheduler(recharge_manager,storage=Path(recharge_data_dir)/'schedules.json')
        def prepare_recharge_account(account):
            saved = unified.get_id(account.pool_login_id)
            if saved is None or saved.status != 'active':
                raise ActivityError('共享账号需要重新登录。', 'APP_LOGIN_REQUIRED', 401)
            try:
                refresh_app_credentials(saved)
            except (AppLoginError, RequestException) as error:
                required = isinstance(error, AppCredentialError) and error.login_required
                raise ActivityError(str(error), 'APP_LOGIN_REQUIRED' if required else 'APP_REFRESH_FAILED',
                                    401 if required else 502, retryable=not required) from error

        recharge = create_recharge_app(manager=recharge_manager, scheduler=recharge_scheduler,
                                      app_login_provider=current_app_login, app_logout_provider=forget_app_login,
                                      app_accounts_provider=unified.public, app_credentials_provider=shared_credentials,credential_cache=credential_cache,
                                      app_prepare_provider=prepare_recharge_account)
        recharge.config.update(SESSION_COOKIE_NAME='recharge_session', SESSION_COOKIE_PATH='/recharge', WORKSPACE_ACCESS_GUARDED=True)
        app.extensions['recharge_app'] = recharge
        app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {'/recharge': recharge})

    def restore_activity_sessions(saved):
        if not enable_recharge:
            return
        from recharge.cmcc import ActivityError
        manager, scheduler = recharge.extensions['accounts'], recharge.extensions['scheduler']
        for linked in list(manager.accounts.values()):
            if linked.pool_login_id != saved.id:
                continue
            if scheduler.snapshots().get(linked.id, {}).get('managed'):
                # Fixed Wednesday workers prepare their own activity session
                # at 07:55. Other businesses do not depend on this restoration.
                continue
            if scheduler.busy(linked.id) or not linked.client.operation_lock.acquire(blocking=False):
                continue
            try:
                linked.client.app_credentials_provider = lambda login_id=saved.id: shared_credentials(login_id)
                snapshot = unified.credentials(saved.id)
                if snapshot is None:
                    continue
                material = linked.client.app_material
                if linked.client.mode != 'app' or not material or material.get('phone') != snapshot['phone']:
                    state = linked.client.connect_app(snapshot['cookie'],snapshot['phone'],snapshot['profile'])
                else:
                    state = linked.client.public_identity(linked.client.cached_user) if linked.client.cached_user is not None else linked.client.identity()
                    if not state.get('authenticated'):
                        state = linked.client.renew_app_session()
                linked.name = saved.name or saved.public()['phone_masked']
                manager.update(linked, {**state, 'session_status':'active'})
            except ActivityError as error:
                manager.update(linked, {'authenticated':False, 'session_status':'retry'}, error=error)
                raise
            finally:
                linked.client.operation_lock.release()

    def maintain_login(saved):
        from recharge.cmcc import ActivityError
        with unified.phone_lock(saved.phone):
            if unified.get(saved.phone) is not saved:
                return True
            if lingxi_scheduler.busy_login(saved.id):
                return False
            if star_scheduler.busy_login(saved.id):
                return False
            if platforms.busy_login(saved.id):
                return False
            if checkin_scheduler.busy_login(saved.id):
                return False
            if monthly_scheduler.busy_login(saved.id):
                return False
            if coupon_uses.busy_login(saved.id):
                return False
            if enable_recharge:
                activity_manager = recharge.extensions['accounts']
                activity_scheduler = recharge.extensions['scheduler']
                if any(a.pool_login_id == saved.id and activity_scheduler.busy(a.id)
                       for a in list(activity_manager.accounts.values())):
                    return False
            if any(a.app_phone == saved.phone and a.lock.locked() for a in all_chat_accounts()):
                return False
        # Only the primary APP refresh takes the phone lock. Business SSO
        # checks use their own sessions and cannot hold up another feature.
        errors = []
        try:
            refresh_app_credentials(saved)
            ensure_token(saved)
        except (LingxiError,AppLoginError,RequestException) as error:
            errors.append(error)
        if not any(isinstance(e,AppCredentialError) and e.login_required for e in errors):
            try:
                restore_activity_sessions(saved)
            except ActivityError as error:
                errors.append(error)
        if errors:
            required = next((e for e in errors if isinstance(e,AppCredentialError) and e.login_required),None)
            if not required:
                unified.mark(saved, 'active','后台维护暂未完成，将自动重试；其他功能可继续使用')
            if required and enable_recharge:
                manager = recharge.extensions['accounts']
                for linked in list(manager.accounts.values()):
                    if linked.pool_login_id == saved.id:
                        manager.update(linked,{'authenticated':False,'session_status':'login_required'},
                            error=ActivityError(str(required),'APP_LOGIN_REQUIRED',401))
            return False
        return True

    def prepare_scheduled_chat(login_id):
        saved=unified.get_id(login_id)
        if saved is None:raise TaskError('账号已退出，请重新登录后预约')
        with unified.phone_lock(saved.phone):
            if saved.status=='login_required':raise TaskError('账号需要重新验证，请打开账号管理')
            try:ensure_token(saved)
            except (AppLoginError,LingxiError,RequestException) as error:
                raise TaskError('账号校验未通过，请校验并重连后重新预约') from error

    def send_scheduled_chat(login_id,content,on_sent):
        with unified.lock:
            saved=unified.get_id(login_id)
            if saved is None or saved.status=='login_required' or not saved.token:
                raise TaskError('共享账号尚未就绪，本次未发送')
            revision=saved.credential_revision;token=saved.token
            target=Account(auth_code=token,verified=True,app_phone=saved.phone)
        # Independent conversations allow the next scheduled request to start
        # while the previous one is still streaming its reply.
        def before_send():
            nonlocal revision,token
            with unified.lock:
                latest=unified.get_id(login_id)
                if latest is None or latest.status=='login_required' or not latest.token:
                    raise TaskError('共享账号已退出或正在更新，本次未发送')
                revision,token=latest.credential_revision,latest.token
                target.auth_code=token
                on_sent()
        try:yield from chat(target,content,before_send=before_send)
        except LingxiError as error:
            if error.authentication:
                with unified.lock:
                    if saved.token==token:
                        saved.token = ''
                        saved.app_context['lingxi_verified_at'] = 0
                        unified.save()
                        unified.mark(saved,'active','定时消息鉴权失败，将在后台维护；其他功能可继续使用',expected_revision=revision)
                        app.extensions['session_maintenance'].due.pop(saved.id,None)
            raise TaskError('灵犀发送或接收失败，已保留收到的内容，本次未自动重发') from error
        except RequestException as error:
            raise TaskError('网络连接中断，已保留收到的内容，本次未自动重发') from error

    def scheduled_account_info(login_id):
        with unified.lock:
            saved=unified.get_id(login_id)
            return saved.public() if saved else None

    lingxi_scheduler=LingxiScheduler(prepare_scheduled_chat,send_scheduled_chat,scheduled_account_info,storage=database)
    app.extensions['lingxi_scheduler']=lingxi_scheduler
    def checkin_credentials(login_id):
        with unified.lock:
            saved = unified.get_id(login_id)
            if saved is None or saved.status == 'login_required':
                raise CheckinError('共享账号需要重新登录，请在账号管理填写验证码。', 'APP_LOGIN_REQUIRED', 401)
            return {**unified.credentials(login_id),'id':login_id}

    checkin_scheduler = CheckinScheduler(checkin_credentials, unified.public, storage=database,
        client_factory=lambda provider:CheckinClient(provider,credential_cache=credential_cache))
    app.extensions['checkin_scheduler'] = checkin_scheduler
    def monthly_credentials(login_id):
        with unified.lock:
            saved = unified.get_id(login_id)
            if saved is None or saved.status == 'login_required':
                raise MonthlyCouponAuthError('共享账号需要重新登录，请在账号管理填写验证码。', 'APP_LOGIN_REQUIRED', 401)
            return {**unified.credentials(login_id),'id':login_id}

    monthly_scheduler = MonthlyCouponScheduler(monthly_credentials, unified.public,
        lambda provider,**kwargs:MonthlyCouponClient(provider,credential_cache=credential_cache,**kwargs),storage=database)
    app.extensions['monthly_coupon_scheduler'] = monthly_scheduler
    def star_credentials(login_id):
        with unified.lock:
            saved = unified.get_id(login_id)
            if saved is None:
                raise StarDayError('共享账号需要重新登录，请在账号管理填写验证码。', 'APP_LOGIN_REQUIRED', 401)
            return {**unified.credentials(login_id), 'id':login_id, 'status':saved.status}

    star_scheduler = StarDayScheduler(star_credentials, unified.public, storage=database)
    app.extensions['star_day_scheduler'] = star_scheduler
    def activity_credentials(login_id):
        with unified.lock:
            saved = unified.get_id(login_id)
            if saved is None or saved.status != 'active':
                return None
            return {**unified.credentials(login_id),'id':login_id}

    platforms = PlatformManager(app, root_dir=Path(activities.__file__).resolve().parent,
                                storage=database, credentials=activity_credentials, accounts=unified.public)
    app.extensions['platform_manager'] = platforms
    app.extensions['session_maintenance'] = SessionMaintenance(unified, maintain_login)
    app.wsgi_app = AccessGate(app.wsgi_app, access_security)
    return app


app = create_app(enable_recharge=True, database_path=DATA_DIR/'accounts.sqlite3',recharge_data_dir=RECHARGE_DATA_DIR)

if __name__ == "__main__":
    port = int(os.environ.get("LINGXI_PORT", "5500"))
    app.extensions['update_checker'].start()
    app.extensions['session_maintenance'].start()
    app.extensions['lingxi_scheduler'].start()
    app.extensions['checkin_scheduler'].start()
    app.extensions['monthly_coupon_scheduler'].start()
    app.extensions['star_day_scheduler'].start()
    app.extensions['platform_manager'].start()
    app.extensions['recharge_app'].extensions['scheduler'].start()
    from network_access import create_http_server
    server = create_http_server(app, port)
    print(f'本地工具台已启动：IPv4 http://127.0.0.1:{server.effective_port}/ · IPv6 http://[::1]:{server.effective_port}/', flush=True)
    server.run()
