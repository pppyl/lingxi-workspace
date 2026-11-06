"""Persistent business switches: request gates and reversible scheduler pauses."""
from functools import wraps
from threading import RLock

from flask import g, jsonify, request

from feature_runtime import FeatureLease, feature_scope, current_feature
from login_storage import LoginStorageError

BUILTINS = (
    ('lingxi', '灵犀对话', '/lingxi', ('/lingxi', '/api/lingxi', '/api/chat', '/api/conversation', '/api/login/select')),
    ('star-day', '星动日', '/star-day', ('/star-day', '/api/star-day')),
    ('recharge', '周三充值日', '/recharge/', ('/recharge',)),
    ('checkin', '签到有礼', '/checkin', ('/checkin', '/api/checkin')),
    ('monthly-coupon', '每月话费券', '/monthly-coupon', ('/monthly-coupon', '/api/monthly-coupon')),
    ('coupons', '我的卡券', '/coupons', ('/coupons', '/api/coupons', '/coupon-verification')),
    ('mobile-rights', '移动权益', '/mobile-rights', ('/mobile-rights', '/api/mobile-rights')),
    ('bills', '信息查询', '/information', ('/information', '/bills', '/api/bills', '/api/usage', '/api/services')),
)


class FeatureControl:
    def __init__(self, app, storage=None):
        self.app, self.storage, self.lock = app, storage, RLock()
        saved = storage.load_activity_data('feature-switches') if storage else None
        if saved is not None and (not isinstance(saved, dict) or any(type(value) is not bool for value in saved.values())):
            raise LoginStorageError('功能开关配置无效，已保留原数据库。')
        self.settings = saved or {}
        order = storage.load_activity_data('feature-order') if storage else None
        if order is not None and (not isinstance(order,list) or any(not isinstance(key,str) for key in order) or len(set(order))!=len(order)):
            raise LoginStorageError('功能排序配置无效，已保留原数据库。')
        self.order = order or []
        self.entries, self.generations = {}, {}
        for key, title, home, paths in BUILTINS:
            self.register(key, title, home, paths)
        app.extensions['feature_control'] = self
        self.install_gate(app)

        @app.get('/api/features')
        def features():
            return jsonify(features=self.public(),default_order=self.default_order())

        @app.post('/api/features/order')
        def save_order():
            payload=request.get_json(silent=True)
            ids=payload.get('feature_ids') if isinstance(payload,dict) else None
            if not isinstance(ids,list) or any(not isinstance(key,str) for key in ids) or len(set(ids))!=len(ids):
                return jsonify(error='请提供不重复的功能顺序。'),400
            with self.lock:
                if set(ids)!=set(self.default_order()):
                    return jsonify(error='功能列表已变化，请刷新后重新排序。'),409
                if self.storage:self.storage.save_activity_data('feature-order',ids)
                self.order=list(ids)
            return jsonify(features=self.public(),default_order=self.default_order(),message='功能顺序已保存，左侧导航已同步。')

        @app.post('/api/features/<feature_id>')
        def switch(feature_id):
            payload = request.get_json(silent=True)
            if (feature_id not in self.entries or not isinstance(payload, dict)
                    or type(payload.get('enabled')) is not bool):
                return jsonify(error='请选择存在的功能并提供有效开关状态。'), 400
            platform = self.entries[feature_id]['platform']
            if payload['enabled'] and platform is not None and platform['state'] != 'running':
                return jsonify(error='该活动已到期或移除，不能重新开启。'), 409
            self.set_enabled(feature_id, payload['enabled'])
            return jsonify(features=self.public(), message=self.entries[feature_id]['title']+
                           ('已开启，原配置与记录继续保留。' if payload['enabled'] else '已关闭，后台任务与业务请求已停用。'))

    def register(self, feature_id, title, home, paths, *, temporary=False, platform=None):
        self.entries[feature_id] = dict(id=feature_id, title=title, home=home,
            paths=tuple(paths), temporary=temporary, platform=platform, schedulers=[])

    def enabled(self, feature_id):
        return self.settings.get(feature_id, True)

    def public(self):
        with self.lock:
            rows = []
            for key, entry in self.entries.items():
                platform = entry['platform']
                state = platform['state'] if platform else 'running'
                rows.append(dict(id=key, title=entry['title'], home=entry['home'],
                    enabled=self.enabled(key), state=state, temporary=entry['temporary'],
                    active=self.enabled(key) and state == 'running', can_delete=platform is not None,
                    navigation=platform.get('navigation','main') if platform else 'main',
                    end_at=platform['metadata'].get('end_at') if platform else None))
            return self.order_rows(rows)

    def default_order(self):
        with self.lock:
            return [key for key,entry in self.entries.items()
                    if entry['platform'] is None or entry['platform']['state']!='deleted']

    def order_rows(self, rows):
        with self.lock:
            positions={key:index for index,key in enumerate(self.order)}
        return sorted(rows,key=lambda row:positions.get(row['id'],len(positions)))

    def set_enabled(self, feature_id, enabled):
        with self.lock:
            if self.enabled(feature_id) == enabled:
                return
            updated = {**self.settings, feature_id:enabled}
            if self.storage:
                self.storage.save_activity_data('feature-switches', updated)
            self.settings = updated
            self.generations[feature_id] = self.generations.get(feature_id, 0)+1
            schedulers = list(self.entries[feature_id]['schedulers'])
        # No scheduler lock is held while persisting or acquiring another lock.
        # An already-sent HTTP request may finish; cancellation stops later rounds.
        for scheduler in schedulers:
            with scheduler.lock:
                if not enabled:
                    for cancellation in getattr(scheduler, 'cancellations', {}).values():
                        cancellation.set()
                else:
                    # Cancellations belong to the old run, not the saved plan.
                    for key, cancellation in list(getattr(scheduler, 'cancellations', {}).items()):
                        if key not in getattr(scheduler, 'workers', {}) and not getattr(scheduler, 'running', set()):
                            scheduler.cancellations.pop(key, None)

    def identify(self, path):
        # Independent channels must win over their /checkin parent.
        candidates = [(len(prefix), key) for key, entry in self.entries.items() for prefix in entry['paths']
                      if path == prefix.rstrip('/') or path.startswith(prefix.rstrip('/')+'/')]
        return max(candidates, default=(0, None))[1]

    def install_gate(self, app, *, fixed_id=None):
        @app.before_request
        def gate_feature():
            feature_id = fixed_id or self.identify(request.path)
            if feature_id is None or request.path.startswith(('/static/', '/activities/')):
                return
            if not self.enabled(feature_id):
                title = self.entries[feature_id]['title']
                if request.path.startswith('/api/') or fixed_id and request.path != '/':
                    response = jsonify(error=title+'已关闭，请到平台管理重新开启。', code='FEATURE_DISABLED', feature_id=feature_id)
                    response.status_code = 423
                    response.headers['X-Workspace-Feature-Disabled'] = feature_id
                    return response
                return self.app.jinja_env.get_template('feature_disabled.html').render(feature_title=title, feature_id=feature_id)
            g.feature_token = current_feature.set(FeatureLease(self, feature_id))

        @app.teardown_request
        def release_feature(_error):
            token = g.pop('feature_token', None)
            if token is not None:
                current_feature.reset(token)

    def bind_scheduler(self, feature_id, scheduler, *, background=(), workers=()):
        self.entries[feature_id]['schedulers'].append(scheduler)
        scheduler.feature_enabled = lambda:self.enabled(feature_id)
        for name in background:
            self._bind(feature_id, scheduler, name, skip_disabled=True)
        for name in workers:
            self._bind(feature_id, scheduler, name, skip_disabled=False)

    def scoped(self, feature_id, *, skip_disabled=False):
        def decorate(original):
            @wraps(original)
            def call(*args, **kwargs):
                if skip_disabled and not self.enabled(feature_id):
                    return False
                inherited = current_feature.get()
                lease = inherited if inherited and inherited.manager is self and inherited.feature_id == feature_id else FeatureLease(self, feature_id)
                with feature_scope(lease):
                    return original(*args, **kwargs)
            return call
        return decorate

    def _bind(self, feature_id, scheduler, name, *, skip_disabled):
        setattr(scheduler, name, self.scoped(feature_id, skip_disabled=skip_disabled)(getattr(scheduler, name)))
