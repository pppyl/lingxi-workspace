from __future__ import annotations

import hmac
import secrets
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request, session

from .cmcc import ActivityError, ENTRY, ROOT
from .accounts import AccountManager
from .scheduler import ClaimScheduler


def create_app(client=None, *, manager=None, scheduler=None, app_login_provider=None, app_logout_provider=None,
               app_accounts_provider=None, app_credentials_provider=None):
    app = Flask(__name__)
    app.config.update(SECRET_KEY=secrets.token_hex(32), MAX_CONTENT_LENGTH=48 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict')
    manager = manager or AccountManager(storage=None if client else ROOT / '.local/accounts.json', default_client=client)
    scheduler = scheduler or ClaimScheduler(manager, storage=None if client else ROOT / '.local/schedules.json')
    app.extensions.update(activity=manager.get().client, accounts=manager, scheduler=scheduler)

    def bind_credentials(account, login_id):
        if app_credentials_provider is not None:
            account.client.app_credentials_provider = lambda: app_credentials_provider(login_id)

    for linked in list(manager.accounts.values()):
        if linked.pool_login_id:
            bind_credentials(linked,linked.pool_login_id)

    def account_for_request():
        data = request.get_json(silent=True) or {}
        return manager.get(request.args.get('account_id') or data.get('account_id'))

    def use_account(method):
        account = account_for_request()
        if scheduler.busy(account.id):
            if method == 'identity':
                return dict(account.cached)
            raise ActivityError('此账号自动任务正在准备或执行，请查看任务记录，完成后再刷新', 'JOB_RUNNING', 409)
        with account.client.operation_lock:
            try:
                result = getattr(account.client, method)()
                manager.update(account, result if isinstance(result, dict) else None)
                return result
            except ActivityError as error:
                manager.update(account, error=error)
                raise

    def require_idle(account):
        if scheduler.busy(account.id):
            raise ActivityError('此账号自动任务正在执行，请先停止任务再调整登录', 'JOB_RUNNING', 409)

    def account_ids(data):
        ids = data.get('account_ids')
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
            raise ActivityError('请勾选要操作的账号', 'INVALID_ACCOUNTS')
        for account_id in ids:
            manager.get(account_id)
        return list(dict.fromkeys(ids))

    @app.before_request
    def local_access():
        if request.host.split(':')[0] not in {'127.0.0.1', 'localhost'}:
            return jsonify(error='仅限本机访问', code='LOCAL_ONLY'), 403
        origin = request.headers.get('Origin')
        if origin and urlsplit(origin).netloc != request.host:
            return jsonify(error='请求来源无效', code='ORIGIN_REJECTED'), 403
        if request.path.startswith('/api/') and request.method == 'POST':
            expected = session.get('csrf', '')
            provided = request.headers.get('X-CSRF-Token', '')
            if not expected or not hmac.compare_digest(expected, provided):
                return jsonify(error='页面验证已失效，请刷新页面', code='CSRF_REJECTED'), 403
            if not request.is_json:
                return jsonify(error='需要 JSON 请求', code='JSON_REQUIRED'), 415
            if not isinstance(request.get_json(silent=True), dict):
                return jsonify(error='需要 JSON 对象', code='JSON_REQUIRED'), 400

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'")
        return response

    @app.errorhandler(ActivityError)
    def activity_error(error):
        return jsonify(error=str(error), code=error.code), error.status

    @app.get('/')
    def index():
        session.setdefault('csrf', secrets.token_urlsafe(32))
        with app.open_resource('static/activity.html', mode='r', encoding='utf-8') as source:
            markup = source.read()
        return render_template('index.html', csrf=session['csrf'], entry=ENTRY,
                               unified_login=app_login_provider is not None, activity_markup=markup)

    @app.get('/api/health')
    def health():
        return jsonify(ok=True, port=int(request.environ.get('SERVER_PORT', 80)))

    @app.get('/api/status')
    def status():
        return jsonify(use_account('identity'))

    @app.get('/api/accounts')
    def accounts():
        return jsonify(accounts=manager.snapshots(), jobs=scheduler.snapshots(),
                       warning=manager.load_error or scheduler.load_error,
                       timezone='Asia/Shanghai')

    @app.get('/api/logins')
    def shared_logins():
        return jsonify(accounts=app_accounts_provider() if app_accounts_provider else [])

    @app.post('/api/login/attach')
    def attach_shared_login():
        data = request.get_json()
        login_id = data.get('login_id')
        if app_login_provider is None or not isinstance(login_id, str) or not login_id:
            raise ActivityError('请从账号管理中选择一个已登录账号', 'APP_LOGIN_REQUIRED', 401)
        material = app_login_provider(login_id)
        if not material:
            raise ActivityError('所选账号已退出，请在账号管理窗口重新登录', 'APP_LOGIN_REQUIRED', 401)
        records = app_accounts_provider() if app_accounts_provider else []
        item = next((r for r in records if r['id'] == login_id), {})
        name = item.get('name') or item.get('phone_masked') or '活动账号'
        job_snapshot = scheduler.snapshots()
        with manager.lock:
            existing = next((a for a in manager.accounts.values() if a.pool_login_id == login_id
                             or a.client.app_material and a.client.app_material.get('phone') == material['phone']), None)
            target = existing
            # An explicit selection can reconnect an old unbound slot without
            # replacing its waiting schedule (legacy metadata had no login ID).
            requested_id = data.get('account_id')
            if target is None and isinstance(requested_id, str):
                requested = manager.get(requested_id)
                if requested.client.mode == 'none' and not requested.pool_login_id:
                    target = requested
            if target is None:
                target = next((a for a in manager.accounts.values() if a.client.mode == 'none'
                               and not a.pool_login_id and not job_snapshot.get(a.id, {}).get('enabled')), None)
            if target is None:
                target = manager.add(name)
            target.pool_login_id = login_id
        # Never wait on an account's operation while holding the directory lock:
        # the background restorer also writes account state after its operation.
        if scheduler.busy(target.id):
            return jsonify(account_id=target.id, state=dict(target.cached), message='已选择正在执行的账号')
        with target.client.operation_lock:
            try:
                bind_credentials(target,login_id)
                material = app_login_provider(login_id)
                if not material:
                    raise ActivityError('共享账号已退出或需要验证', 'APP_LOGIN_REQUIRED',401)
                if target.client.app_material and target.client.app_material.get('cookie') == material['cookie']:
                    state = target.client.identity()
                else:
                    state = target.client.connect_app(**material)
                target.name = name
                target.pool_login_id = login_id
                manager.update(target, {**state, 'session_status':'active'})
            except ActivityError as error:
                manager.update(target, {'authenticated':False, 'session_status':'retry' if error.status != 401 else 'login_required'}, error=error)
                raise
        return jsonify(account_id=target.id, state=state, message='已连接所选共享账号')

    @app.post('/api/accounts/add')
    def add_account():
        account = manager.add(request.get_json().get('name'))
        return jsonify(account=account.snapshot())

    @app.post('/api/accounts/remove')
    def remove_account():
        account = account_for_request()
        require_idle(account)
        scheduler.remove(account.id)
        manager.remove(account.id)
        return jsonify(message='账号已移除，会话已清除')

    @app.post('/api/login')
    def login():
        if app_login_provider is not None:
            raise ActivityError('请在统一登录页面登录，活动复用同一份 APP Cookie', 'UNIFIED_LOGIN_REQUIRED', 409)
        data = request.get_json()
        account = account_for_request()
        require_idle(account)
        with account.client.operation_lock:
            result = account.client.login_app(data.get('phone'), data.get('code'))
            manager.update(account, result)
        return jsonify(**result, message='APP 登录及活动会话验证成功')

    @app.post('/api/login/sms')
    def login_sms():
        if app_login_provider is not None:
            raise ActivityError('请在统一登录页面获取验证码，活动无需重复登录', 'UNIFIED_LOGIN_REQUIRED', 409)
        account = account_for_request()
        require_idle(account)
        with account.client.operation_lock:
            return jsonify(account.client.send_sms(request.get_json().get('phone')))

    @app.post('/api/login/current')
    def current_app_login():
        if app_login_provider is None:
            raise ActivityError('请使用手机号和验证码登录 APP', 'APP_LOGIN_REQUIRED', 401)
        material = app_login_provider()
        if not material:
            raise ActivityError('请先在灵犀页面使用手机号和验证码登录一次', 'APP_LOGIN_REQUIRED', 401)
        login_id = None
        if app_credentials_provider is not None:
            for item in app_accounts_provider() if app_accounts_provider else []:
                try:
                    latest = app_credentials_provider(item['id'])
                except ActivityError:
                    continue
                if latest and latest.get('phone') == material.get('phone'):
                    login_id = item['id']
                    break
            if login_id is None:
                raise ActivityError('共享账号已退出，请重新选择账号', 'APP_LOGIN_REQUIRED',401)
        account = account_for_request()
        require_idle(account)
        for other in manager.accounts.values():
            if other.id != account.id and other.client.app_material and other.client.app_material.get('phone') == material.get('phone'):
                raise ActivityError(f'该手机号已连接到“{other.name}”，请选择该账号，避免重复任务', 'DUPLICATE_PHONE', 409)
        with account.client.operation_lock:
            if login_id:
                bind_credentials(account,login_id)
                material = app_login_provider(login_id)
                if not material:
                    raise ActivityError('共享账号已退出或需要验证', 'APP_LOGIN_REQUIRED',401)
            result = account.client.connect_app(**material)
            if login_id:
                account.pool_login_id = login_id
            manager.update(account, result)
        return jsonify(**result, message='已使用当前 APP 账号建立活动会话')

    @app.post('/api/import')
    def import_session():
        if app_login_provider is not None:
            raise ActivityError('请使用统一 APP 登录，无需导入网页 Cookie', 'UNIFIED_LOGIN_REQUIRED', 409)
        data = request.get_json()
        account = account_for_request()
        require_idle(account)
        with account.client.operation_lock:
            account.client.import_session(data.get('cookie'), data.get('const_id', ''), data.get('url'))
            result = account.client.identity()
            manager.update(account, result)
            return jsonify(result)

    @app.post('/api/refresh')
    def refresh():
        return jsonify(use_account('refresh'))

    @app.get('/api/prizes')
    def prizes():
        return jsonify(prizes=use_account('prizes'))

    @app.post('/api/claim')
    def claim():
        account = account_for_request()
        activity = account.client
        if scheduler.busy(account.id):
            raise ActivityError('此账号自动任务正在运行，无需手动重复提交', 'JOB_RUNNING', 409)
        # A concurrent request gets a busy response instead of waiting and submitting again.
        if not activity.operation_lock.acquire(blocking=False):
            raise ActivityError('其他操作正在处理，请稍后刷新结果', 'CLAIM_BUSY', 409)
        try:
            result = activity.claim()
            manager.update(account, {'authenticated': True})
            return jsonify(result)
        except ActivityError as error:
            manager.update(account, error=error)
            raise
        finally:
            activity.operation_lock.release()

    @app.post('/api/logout')
    def logout():
        account = account_for_request()
        require_idle(account)
        material = account.client.app_material
        if app_logout_provider is not None and material:
            app_logout_provider(material['phone'])
        else:
            scheduler.cancel([account.id])
            with account.client.operation_lock:
                account.client.logout()
        manager.update(account, {'authenticated': False, 'mode': 'none', 'phone': ''})
        return jsonify(message='本地会话已清除')

    @app.post('/api/accounts/batch')
    def batch_accounts():
        data = request.get_json()
        ids = account_ids(data)
        operation = data.get('operation')
        if operation != 'refresh':
            raise ActivityError('不支持此批量操作', 'INVALID_OPERATION')

        def perform(account_id):
            account = manager.get(account_id)
            try:
                require_idle(account)
                with account.client.operation_lock:
                    result = account.client.refresh()
                    manager.update(account, result)
                    return {'account_id': account_id, 'ok': True, 'state': result}
            except ActivityError as error:
                manager.update(account, error=error)
                return {'account_id': account_id, 'ok': False, 'error': str(error)}

        with ThreadPoolExecutor(max_workers=len(ids)) as pool:
            results = list(pool.map(perform, ids))
        return jsonify(results=results)

    @app.post('/api/schedules')
    def schedules():
        data = request.get_json()
        ids = account_ids(data)
        jobs = scheduler.schedule(ids, data, immediate=data.get('immediate') is True)
        return jsonify(jobs=jobs, message='已为选中账号启动任务' if data.get('immediate') else '定时任务已保存')

    @app.post('/api/schedules/cancel')
    def cancel_schedules():
        scheduler.cancel(account_ids(request.get_json()))
        return jsonify(message='已停止选中账号的后续尝试')

    return app
