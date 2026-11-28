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
from workspace.http_transport import RequestException

from features.lingxi.client import Account, LingxiError, chat, exchange_cookie, verify_token
from workspace.accounts.login import (AppLoginError, AppLoginSession, AppCredentialError, SMS_VALID_SECONDS,
                       SLIDER_REQUIRED_CODE, LOGIN_SLIDER_APP_ID, LOGIN_SLIDER_DEVICE_PATH)
from workspace.accounts.pool import SessionMaintenance, UnifiedLogins
from workspace.accounts.storage import DatabaseLoginStorage, LoginStorageError
from features.information import register_information
from features.mobile_rights import register_mobile_rights
from features.member_benefits.service import register_member_benefits
from features.net_age.service import register_net_age
from recharge.cmcc import ActivityError
from features.lingxi.scheduler import LingxiScheduler, TaskError
from features.checkin.beans import CheckinBeans
from features.checkin.client import CheckinError, CheckinClient
from features.checkin.scheduler import CheckinScheduler, CheckinTaskError
from features.monthly_coupon.auth import MonthlyCouponAuthError
from features.monthly_coupon.client import MonthlyCouponClient
from features.monthly_coupon.scheduler import MonthlyCouponScheduler, MonthlyTaskError
from features.star_day.client import StarDayError
from features.star_day.scheduler import StarDayScheduler
from workspace.accounts.credentials import CredentialCache
from workspace.platform_manager import PACKAGE_UPLOAD_PATH, PlatformManager
from workspace.feature_control import FeatureControl
import activities
from features.coupons.client import CouponError
from features.coupons.use import CouponUseManager
from features.coupons.native_device import NativeRechargeDevices
from features.coupons.native_captcha import RechargeCaptcha
from features.coupons.notices import CouponNotices
from features.coupons.captcha import register_coupon_captcha
from runtime_paths import DATA_DIR,RECHARGE_DATA_DIR,prepare_runtime_data
from workspace.access_security import AccessGate, register_access
from workspace.update_checker import register_updates
from workspace.network_access import register_network
from features.monthly_coupon.routes import register_monthly_coupon_routes
from features.star_day.routes import register_star_day_routes
from features.lingxi.routes import register_lingxi_tasks_routes
from features.checkin.routes import register_checkin_routes
from features.coupons.routes import register_coupons_routes


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
    features = FeatureControl(app, database)
    access_security = register_access(app, database)
    network = register_network(app, database)
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
            target.app_cookie, target.app_phone, target.app_profile = saved.cookie, saved.phone, dict(saved.profile)
            target.auth_code, target.verified = saved.token, bool(saved.token)
            return target

    def apply_login(current, record):
        target = state_for(record)
        # Updating credentials never resets this phone's conversation session.
        target.auth_code, target.verified = record.token, bool(record.token)
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

    def app_verification_busy(login_id, *, relogin=False):
        return (app.extensions['services_queries'].cancellations.busy_login(login_id)
                or app.extensions['net_age'].busy_login(login_id)
                or coupon_uses.busy_login(login_id, discard_previews=relogin))

    def refresh_app_credentials(saved, *, force=False, expired_cookie=None):
        with unified.phone_lock(saved.phone):
            if app_verification_busy(saved.id):
                return
            snapshot = unified.credentials(saved.id)
            if snapshot is None:
                raise AppCredentialError('账号已退出，请重新选择账号。',login_required=True)
            if expired_cookie is not None and snapshot['cookie'] != expired_cookie:
                return  # Another request already replaced the rejected primary session.
            if not force and time.time() - snapshot['app_context'].get('refreshed_at',0) < 600:
                return
            revision = snapshot['credential_revision']
            flow = AppLoginSession.from_credentials(snapshot)
            try:
                if expired_cookie is not None:
                    flow.refresh(force_identity=True)
                else:
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

    @features.scoped('lingxi', skip_disabled=True)
    def ensure_token(saved, *, refresh=False):
        # Only APP renewal needs the shared phone lock. Slow Lingxi verification
        # uses its own lock so wallet/OTP operations remain independent.
        if refresh or saved.app_context.get('device_id'):
            refresh_app_credentials(saved,force=refresh)
        with accounts_lock:
            token_lock = lingxi_auth_locks.setdefault(saved.id,RLock())
        # Repeat only if another request rotated the input credentials while
        # Lingxi was checking them; a Lingxi failure never forces APP renewal.
        for _ in range(2):
            with token_lock:
                snapshot = unified.credentials(saved.id)
                if snapshot is None:
                    raise AppCredentialError('账号已退出。',login_required=True)
                revision = snapshot['credential_revision']
                with unified.lock:
                    token = saved.token
                needs_check = not token or refresh or time.time()-saved.app_context.get('lingxi_verified_at',0) >= 600
                if needs_check and not refresh and saved.app_context.get('lingxi_retry_at',0) > time.time():
                    raise LingxiError(saved.app_context.get('lingxi_error_message') or '灵犀会话正在等待重连，其他功能可继续使用。',
                                      code=saved.app_context.get('lingxi_error_code', 'LINGXI_PENDING'))
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
                            with unified.lock:
                                if unified.get(saved.phone) is saved and saved.token == token:
                                    saved.token = ''
                                    saved.app_context['lingxi_verified_at'] = 0
                                    unified.save()
                            token = exchange_cookie(snapshot['cookie'])
                            verify_token(token)
                        verified_at = time.time()
                    else:
                        verified_at = saved.app_context.get('lingxi_verified_at',0)
                except (LingxiError, RequestException) as exc:
                    with unified.lock:
                        if unified.get(saved.phone) is saved and saved.credential_revision != revision:
                            continue
                        if unified.get(saved.phone) is saved and saved.credential_revision == revision:
                            saved.app_context.update(lingxi_retry_at=time.time()+60,
                                lingxi_error_code=exc.code if isinstance(exc,LingxiError) else 'NETWORK',
                                lingxi_error_message=str(exc) if isinstance(exc,LingxiError) else '灵犀服务连接暂未完成，请稍后重试。')
                            unified.mark(saved,'active','APP 登录有效；灵犀暂未就绪，其他功能可继续使用',expected_revision=revision)
                            unified.save()
                    sync_chat_credentials(saved)
                    raise
                with unified.lock:
                    if unified.get(saved.phone) is not saved:
                        raise AppCredentialError('账号已退出。',login_required=True)
                    if saved.credential_revision != revision:
                        continue
                    changed = saved.token != token
                    saved.token = token
                    changed = changed or saved.app_context.get('lingxi_verified_at') != verified_at
                    saved.app_context['lingxi_verified_at'] = verified_at
                    for key in ('lingxi_retry_at', 'lingxi_error_code', 'lingxi_error_message'):
                        changed = changed or key in saved.app_context
                        saved.app_context.pop(key, None)
                    unified.mark(saved,'active','APP 会话已维护，业务使用最新凭据',expected_revision=revision)
                    if changed:
                        unified.save()
                sync_chat_credentials(saved)
                return
        raise AppCredentialError('账号凭据更新中，请稍后重试。')

    def reuse_login(current, saved, activate=True):
        if activate:
            ensure_token(saved)
        elif saved.app_context.get('device_id'):
            refresh_app_credentials(saved)
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
            if request.path == PACKAGE_UPLOAD_PATH:
                if origin and origin != request.host_url.rstrip('/'):
                    return jsonify(error="请求来源不正确。"), 403
                return None  # The local package endpoint accepts a bounded ZIP body.
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

    def notice_snapshots():
        with unified.lock:
            return [{**unified.credentials(saved.id),'id':saved.id} for saved in unified.records.values()]

    coupon_notices=CouponNotices(notice_snapshots,credential_cache,database)
    app.extensions['coupon_notices']=coupon_notices

    def coupon_credentials(login_id):
        with unified.lock:
            saved = unified.get_id(login_id)
            if saved is None:
                raise CouponError('请选择共享账号池中的账号。', 'ACCOUNT_NOT_FOUND', 404)
            if saved.status != 'active':
                raise CouponError('此账号尚未就绪，请从左侧账号管理恢复登录。', 'APP_LOGIN_REQUIRED', 401)
            return {**unified.credentials(login_id),'id':login_id}

    def recover_coupon_app(login_id, snapshot):
        saved = unified.get_id(login_id)
        if saved is None:
            raise CouponError('账号已退出，请重新选择账号。','ACCOUNT_NOT_FOUND',404)
        with unified.phone_lock(saved.phone):
            if app_verification_busy(login_id):
                raise CouponError('该账号正在验证码或用券流程中，请完成后再恢复登录。','ACCOUNT_BUSY',409)
            try:
                refresh_app_credentials(saved,force=True,expired_cookie=snapshot['cookie'])
            except AppLoginError as error:
                required = isinstance(error,AppCredentialError) and error.login_required
                raise CouponError(str(error),'APP_LOGIN_REQUIRED' if required else 'APP_REFRESH_FAILED',401 if required else 502) from error

    # The wallet entry's public deep-link channel applies independently of
    # the account's private APP login cookie.
    coupon_recharge_devices=NativeRechargeDevices(coupon_credentials,database)
    coupon_uses = CouponUseManager(coupon_credentials, storage=database,
        payment_credentials_provider_by_id=unified.credentials,
        wallet_entry='https://touch.10086.cn/i/mobile/mycoupons.html?channelId=P00000004912',credential_cache=credential_cache,
        app_session_recover=recover_coupon_app,
        recharge_devices=coupon_recharge_devices,
        recharge_captcha=RechargeCaptcha(devices=coupon_recharge_devices.legacy))
    app.extensions['coupon_uses'] = coupon_uses

    @app.errorhandler(LoginStorageError)
    def account_database_error(error):
        return jsonify(error=str(error)), 503

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
        flow = None
        held_lock = login_operation_lock(current, manage)
        if not held_lock.acquire(blocking=False):
            return jsonify(error="正在登录或接收回复，请稍候。"), 409
        try:
            with unified.phone_lock(phone):
                saved = unified.get(phone)
                if relogin and saved is not target:
                    return jsonify(error='账号已退出，请重新添加账号。'), 404
                reauthenticate = saved and (saved.status == 'login_required' or relogin and value.get('force') is True)
                if saved and app_verification_busy(saved.id, relogin=bool(relogin and reauthenticate)):
                    return jsonify(error='该账号正在用券或业务退订验证中，请完成后再重新登录。', code='ACCOUNT_BUSY'), 409
                if saved and not reauthenticate:
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
                if flow is None or time.monotonic() - flow.sms_sent_at >= SMS_VALID_SECONDS:
                    device_id = flow.device_id if flow is not None else saved.app_context.get('device_id') if saved else None
                    flow = AppLoginSession()
                    if device_id:
                        flow.device_id = device_id
                    with unified.lock:
                        unified.pending[phone] = flow
                with accounts_lock:
                    login_flows[session['sid']] = flow
                request_id = value.get('sms_request_id')
                resend = value.get('resend') is True and (request_id is None or request_id == flow.sms_request_id)
                if flow.phone == phone and not resend:
                    return jsonify(**flow.sms_status(reused=True))
                return jsonify(**flow.send_sms(phone))
        except AppLoginError as exc:
            return jsonify(error=str(exc), code=exc.code, retry_after=exc.retry_after), 400
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
        flow = None
        lingxi_warning = ''
        try:
            with unified.phone_lock(phone):
                saved = unified.get(phone)
                if relogin and saved is not target:
                    return jsonify(error='账号已退出，请重新添加账号。'), 404
                reauthenticate = saved and (saved.status == 'login_required' or relogin and value.get('force') is True)
                if saved and app_verification_busy(saved.id, relogin=bool(relogin and reauthenticate)):
                    return jsonify(error='该账号正在用券或业务退订验证中，请完成后再重新登录。', code='ACCOUNT_BUSY'), 409
                if saved and not reauthenticate:
                    if manage:
                        current = reuse_login(current, saved, activate=False)
                else:
                    with unified.lock:
                        pending = unified.pending.get(phone)
                    request_id = value.get('sms_request_id')
                    if request_id is not None and (pending is None or request_id != pending.sms_request_id):
                        latest = (pending.sms_status(reused=True) if pending is not None and pending.phone == phone
                                  and time.monotonic() - pending.sms_sent_at < SMS_VALID_SECONDS else None)
                        message = '验证码已更新，请填写最近收到的验证码。' if latest else '本次验证码已失效，请重新获取。'
                        return jsonify(error=message, code='SMS_FLOW_CHANGED', sms=latest), 409
                    flow = pending if pending is not None else login_flow()
                    slider_token = value.get('slider_token')
                    cookie = (flow.login(phone, code, slider_token=slider_token) if slider_token
                              else flow.login(phone, code))
                    profile = dict(flow.user_info) if isinstance(flow.user_info, dict) else {}
                    context = flow.export_context()
                    with unified.lock:
                        previous_context = saved.app_context if saved else {}
                        if isinstance(context, dict):
                            context = {**{key: value for key, value in previous_context.items() if key.startswith('lingxi_')},
                                       **context}
                        saved = unified.remember(phone, cookie, profile, saved.token if saved else '', name.strip(),
                                                 app_context=context if isinstance(context,dict) else None)
                    sync_chat_credentials(saved)
                    app.extensions['session_maintenance'].due.pop(saved.id, None)
                    platforms.login_restored(saved.id)
                    if restored := app.extensions.get('net_age_surprise_login_restored'):
                        restored(saved.id)
            if not manage:
                try:
                    current = reuse_login(current, saved)
                except (LingxiError, RequestException) as exc:
                    current = apply_login(current, saved)
                    lingxi_warning = ('APP 登录已成功并保存；灵犀暂未就绪，其他功能可继续使用。' +
                                      (str(exc) if isinstance(exc, LingxiError) else '灵犀网络连接暂未完成，请稍后重试。'))
            with accounts_lock:
                login_flows.pop(session["sid"], None)
            if manage:
                message = '账号已重新登录，任务绑定和备注已保留。' if relogin else '账号已加入共享账号池。'
                if features.enabled('lingxi') and not saved.token:
                    message += '灵犀将在使用时独立连接，不影响其他功能。'
                return jsonify(account=saved.public(), message=message, app_logged_in=True, lingxi_ready=bool(saved.token))
            return jsonify(**current.status(), login_id=saved.id, app_logged_in=True,
                           lingxi_ready=bool(saved.token), warning=lingxi_warning)
        except AppLoginError as exc:
            result = {'error': str(exc), 'code': exc.code}
            if exc.code == SLIDER_REQUIRED_CODE and flow is not None:
                result['slider_required'] = flow.slider_required
                if flow.slider_required:
                    result['slider_challenge'] = {'app_id': LOGIN_SLIDER_APP_ID,
                                                  'device_path': LOGIN_SLIDER_DEVICE_PATH}
            return jsonify(result), 400
        except RequestException:
            return jsonify(error="连接登录服务失败，请稍后重试。"), 502
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
            warning = ''
            if not features.enabled('lingxi'):
                refresh_app_credentials(saved,force=True)
            else:
                try:
                    ensure_token(saved,refresh=True)
                except (LingxiError, RequestException) as exc:
                    warning = str(exc) if isinstance(exc, LingxiError) else '灵犀网络连接暂未完成。'
            restore_activity_sessions(saved)
            return jsonify(account=saved.public(), lingxi_ready=bool(saved.token), warning=warning,
                           message='APP 登录已核对，灵犀暂未就绪；其他关联会话已核对。'+warning if warning else '登录态有效，关联活动会话已恢复')
        except (LingxiError,AppLoginError) as exc:
            return jsonify(error=str(exc)), 400
        except RequestException:
            return jsonify(error='连接验证服务失败，请稍后重试。'), 502
        except ActivityError as exc:
            return jsonify(error=str(exc)), exc.status

    def clear_login_states(phone):
        saved=unified.get(phone)
        if saved is not None:
            app.extensions['mobile_rights'].forget(saved.id)
            platforms.forget(saved.id)
            star_scheduler.forget(saved.id)
            monthly_scheduler.forget(saved.id)
            checkin_scheduler.forget(saved.id)
            lingxi_scheduler.cancel_login(saved.id)
            credential_cache.forget(saved.id)
            coupon_uses.reminders.forget(saved.id)
            coupon_uses.recharge_devices.forget(saved.id)
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
        if app.extensions['mobile_rights'].busy_login(saved.id):
            return jsonify(error='该账号正在连接权益会话、领取权益或发送活动验证码，请完成后退出。'),409
        if enable_recharge:
            from recharge.cmcc import ActivityError
            try:
                forget_app_login(saved.phone)
            except ActivityError as exc:
                return jsonify(error=str(exc)), exc.status
        else:
            if app_verification_busy(saved.id):
                return jsonify(error='该账号正在用券或业务退订验证中，请完成后退出。', code='ACCOUNT_BUSY'), 409
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
            rights_saved=unified.get(current.app_phone)
            if rights_saved is not None and app.extensions['mobile_rights'].busy_login(rights_saved.id):
                return jsonify(error='该账号正在连接权益会话、领取权益或发送活动验证码，请完成后退出。'),409
            if enable_recharge:
                try:
                    forget_app_login(current.app_phone)
                except Exception as exc:
                    from recharge.cmcc import ActivityError
                    if isinstance(exc, ActivityError):
                        return jsonify(error=str(exc)), exc.status
                    raise
            else:
                saved = unified.get(current.app_phone)
                if saved is not None and app_verification_busy(saved.id):
                    return jsonify(error='该账号正在用券或业务退订验证中，请完成后退出。', code='ACCOUNT_BUSY'), 409
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
                if not saved.token or saved.status != 'active' or time.time() - saved.checked_at > 300:
                    ensure_token(saved)
                with unified.lock:
                    current.auth_code, current.verified = saved.token, bool(saved.token)
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
            if saved is not None and app.extensions['mobile_rights'].busy_login(saved.id):
                raise ActivityError('该账号正在连接权益会话、领取权益或发送活动验证码，请完成后退出。','ACCOUNT_BUSY',409)
            if saved is not None and app_verification_busy(saved.id):
                raise ActivityError('该账号正在用券或业务退订验证中，请完成后退出。', 'ACCOUNT_BUSY', 409)
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

        if database is not None or recharge_data_dir is not None:
            activity_data=Path(recharge_data_dir) if recharge_data_dir is not None else Path(database_path).resolve().parent/'recharge'
            if recharge_manager is None:
                recharge_manager = AccountManager(storage=activity_data/'accounts.json', database=database)
            if recharge_scheduler is None:
                from recharge.scheduler import ClaimScheduler
                recharge_scheduler=ClaimScheduler(recharge_manager,storage=activity_data/'schedules.json')
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
        features.install_gate(recharge, fixed_id='recharge')
        recharge.add_template_global(network.sidebar, name='workspace_network')
        app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {'/recharge': recharge})

    @features.scoped('recharge', skip_disabled=True)
    def restore_activity_sessions(saved):
        if not enable_recharge or not features.enabled('recharge'):
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
            if app_verification_busy(saved.id):
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
            if features.enabled('lingxi'):
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
        def ready_credentials():
            with unified.lock:
                current=unified.get_id(login_id)
                if current is None:
                    raise TaskError('共享账号已退出，本次未发送')
                if current.status=='login_required':
                    raise TaskError('共享账号需要重新验证，本次未发送')
                missing=not current.token
            if missing:
                try:ensure_token(current)
                except (AppLoginError,LingxiError,RequestException) as error:
                    raise TaskError('共享账号恢复未完成，本次未发送') from error
            with unified.lock:
                current=unified.get_id(login_id)
                if current is None or current.status=='login_required' or not current.token:
                    raise TaskError('共享账号恢复未完成，本次未发送')
                return current,current.credential_revision,current.token

        saved,revision,token=ready_credentials()
        with unified.lock:
            target=Account(auth_code=token,verified=True,app_phone=saved.phone)
        # Independent conversations allow the next scheduled request to start
        # while the previous one is still streaming its reply.
        def before_send():
            nonlocal saved,revision,token
            saved,revision,token=ready_credentials()
            with unified.lock:
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
            if saved is None:
                raise CheckinError('共享账号需要重新登录，请在账号管理填写验证码。', 'APP_LOGIN_REQUIRED', 401)
            snapshot = {**unified.credentials(login_id), 'id': login_id, 'primary_status': saved.status}
            if saved.status == 'login_required' and credential_cache.load('qwhdmark:1021122301', snapshot) is None:
                raise CheckinError('APP 及签到活动均需要恢复登录，请在账号管理验证。', 'APP_LOGIN_REQUIRED', 401)
            return snapshot

    def checkin_accounts():
        result = unified.public()
        for item in result:
            if item['status'] == 'login_required':
                snapshot = unified.credentials(item['id'])
                if snapshot is not None and credential_cache.load('qwhdmark:1021122301', {**snapshot, 'id': item['id']}) is not None:
                    item.update(status='active', activity_session_valid=True, app_login_required=True)
        return result

    checkin_scheduler = CheckinScheduler(checkin_credentials, checkin_accounts, storage=database,
        client_factory=lambda provider:CheckinClient(provider,credential_cache=credential_cache))
    app.extensions['checkin_scheduler'] = checkin_scheduler
    def bean_query(login_id, kind):
        from features.information.bills import BillError
        from features.information.usage import UsageError
        from features.information.services import ServicesError
        try:
            if kind == 'usage':
                return app.extensions['usage_queries'].query(login_id)
            if kind == 'overview':
                app.extensions['usage_queries'].query(login_id)
                app.extensions['services_queries'].query(login_id)
            queries = app.extensions['bill_queries']
            result = queries.query(login_id, queries.months()[0])
            if kind == 'balance' and result['balance'] is None:
                raise CheckinError('未查到真实余额，未提交任务完成记录。', 'BALANCE_QUERY_FAILED')
            return result
        except (BillError, UsageError, ServicesError) as error:
            raise CheckinError(str(error), error.code, error.status) from error

    from features.checkin.video import CheckinVideo
    from features.checkin.cloud import CheckinCloud
    from features.checkin.migu import CheckinMigu
    from features.checkin.baidu_map import CheckinBaiduMap
    from features.checkin.taobao import CheckinTaobao
    from features.checkin.cainiao import CheckinCainiao
    from features.checkin.kuaishou import CheckinKuaishou
    checkin_beans = CheckinBeans(checkin_scheduler, database, query=bean_query,
        video_factory=lambda login_id: CheckinVideo(lambda:checkin_credentials(login_id), credential_cache,
            recover=lambda snapshot:app.extensions['app_identity_recover'](snapshot)),
        cloud_factory=lambda login_id: CheckinCloud(lambda:checkin_credentials(login_id), credential_cache,
            recover=lambda snapshot:app.extensions['app_identity_recover'](snapshot)),
        migu_factory=lambda login_id: CheckinMigu(lambda:checkin_credentials(login_id), credential_cache),
        baidu_map_factory=lambda login_id: CheckinBaiduMap(lambda:checkin_credentials(login_id), credential_cache),
        taobao_factory=lambda login_id: CheckinTaobao(lambda:checkin_credentials(login_id), credential_cache),
        cainiao_factory=lambda login_id: CheckinCainiao(lambda:checkin_credentials(login_id), credential_cache),
        kuaishou_factory=lambda login_id: CheckinKuaishou(lambda:checkin_credentials(login_id), credential_cache))
    checkin_scheduler.beans = checkin_beans
    app.extensions['checkin_beans'] = checkin_beans
    from features.checkin.partner_logins import register_partner_logins
    register_partner_logins(app,unified.public,unified.credentials,credential_cache,checkin_beans)
    from features.checkin.teams import CheckinTeams
    checkin_teams = CheckinTeams(checkin_scheduler)
    checkin_scheduler.team_awards = checkin_teams
    app.extensions['checkin_teams'] = checkin_teams
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

    register_monthly_coupon_routes(app, monthly_scheduler)
    register_star_day_routes(app, star_scheduler)
    register_lingxi_tasks_routes(app, lingxi_scheduler)
    register_checkin_routes(app, checkin_scheduler, checkin_beans, checkin_teams, unified)
    register_coupons_routes(app, unified, coupon_uses, coupon_notices)

    def activity_credentials(login_id, *, allow_cached=False):
        with unified.lock:
            saved = unified.get_id(login_id)
            if saved is None or not allow_cached and saved.status != 'active':
                return None
            return {**unified.credentials(login_id),'id':login_id,'primary_status':saved.status}

    def recover_activity_identity(snapshot):
        saved = unified.get(snapshot['phone'])
        if saved is None:
            raise AppCredentialError('账号已退出，请重新选择账号。',login_required=True)
        with unified.phone_lock(saved.phone):
            if app_verification_busy(saved.id) or monthly_scheduler.busy_login(saved.id):
                raise AppCredentialError('该账号正在用券或短信验证中，请完成后再恢复 APP 会话。')
            refresh_app_credentials(saved,force=True,expired_cookie=snapshot['cookie'])
    app.extensions['app_identity_recover'] = recover_activity_identity

    def accept_bill_credentials(snapshot, flow):
        updated = unified.rotate_credentials(snapshot['phone'], expected_revision=snapshot['credential_revision'],
            cookie=flow.cookie, app_context=flow.export_context(), profile=flow.user_info)
        if updated is not None:
            sync_chat_credentials(updated)

    register_information(app, unified.public, unified.credentials, recover=recover_activity_identity,
                         accept_credentials=accept_bill_credentials)
    register_member_benefits(app, unified.credentials, credential_cache, recover=recover_activity_identity)
    register_net_age(app, lambda login_id:activity_credentials(login_id, allow_cached=True),
                     unified.public, credential_cache, database, recover_activity_identity)
    register_mobile_rights(app, unified.public, unified.credentials, storage=database, cache=credential_cache,
                           recover=recover_activity_identity, accept_credentials=accept_bill_credentials)

    platforms = PlatformManager(app, root_dir=Path(activities.__file__).resolve().parent,
                                package_dir=Path(database_path).resolve().parent/'activity-plugins' if database_path is not None else None,
                                storage=database, credentials=activity_credentials, accounts=unified.public, features=features)
    app.extensions['platform_manager'] = platforms
    features.bind_scheduler('lingxi', lingxi_scheduler, background=('tick',), workers=('_worker','_send_one'))
    features.bind_scheduler('checkin', checkin_scheduler, background=('tick','check_startup','_enqueue'), workers=('_perform','_refresh'))
    features.bind_scheduler('checkin', checkin_beans, background=('tick','queue'), workers=('_perform_tasks',))
    features.bind_scheduler('monthly-coupon', monthly_scheduler, background=('tick','check_startup','_queue'), workers=('_perform',))
    features.bind_scheduler('star-day', star_scheduler, background=('tick','_queue'), workers=('_perform','calibrate'))
    features.bind_scheduler('net-age', app.extensions['net_age_surprise_scheduler'],
        background=('tick','login_restored'),workers=('_perform',))
    if enable_recharge:
        features.bind_scheduler('recharge', recharge.extensions['scheduler'],
            background=('tick','_sync_automatic'), workers=('_worker','_run_attempts'))
    from workspace.navigation_status import register_navigation_status
    issue_sources = {'lingxi': (lingxi_scheduler,'jobs'), 'checkin': (checkin_scheduler,'data'),
        'monthly': (monthly_scheduler,'data'), 'star': (star_scheduler,'data'), 'coupons': (coupon_uses,'data'),
        'coupon_reminders': (coupon_uses.reminders,'data')}
    if enable_recharge:
        issue_sources['recharge'] = (recharge.extensions['scheduler'],'jobs')
        issue_sources['wed_checkin'] = (recharge.extensions['continuous_checkin'],'data')
    register_navigation_status(app,unified.public,issue_sources,platforms)
    app.extensions['session_maintenance'] = SessionMaintenance(unified, maintain_login)
    app.wsgi_app = AccessGate(app.wsgi_app, access_security)
    return app


prepare_runtime_data()
app = create_app(enable_recharge=True, database_path=DATA_DIR/'accounts.sqlite3',recharge_data_dir=RECHARGE_DATA_DIR)

if __name__ == "__main__":
    app.extensions['update_checker'].start()
    app.extensions['session_maintenance'].start()
    app.extensions['lingxi_scheduler'].start()
    app.extensions['checkin_scheduler'].start()
    app.extensions['monthly_coupon_scheduler'].start()
    app.extensions['star_day_scheduler'].start()
    app.extensions['net_age_surprise_scheduler'].start()
    app.extensions['platform_manager'].start()
    app.extensions['recharge_app'].extensions['scheduler'].start()
    from workspace.network_access import create_http_server
    server = create_http_server(app)
    print(f'本地工具台已启动：IPv4 http://127.0.0.1:{server.effective_port}/ · IPv6 http://[::1]:{server.effective_port}/', flush=True)
    server.run()
