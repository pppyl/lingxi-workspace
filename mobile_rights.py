"""Shared-account mobile rights, New Youth coupons and AI bean operations."""
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import dataclass, field
import re
import secrets
from threading import RLock

from flask import jsonify, render_template, request

from credential_cache import CredentialCache
from http_transport import RequestException
from login_storage import LoginStorageError
from mobile_rights_auth import SCOPE, MobileRightsError, wap_session
from mobile_rights_client import RightsClient
from mobile_rights_points import PointClaim, PointClaimError
from mobile_rights_youth import NewYouthClient, XQNError


@dataclass
class RightsContext:
    login_id: str
    phone: str = field(repr=False)
    session: object = field(repr=False)
    rights: object = None
    youth: object = None
    points: object = None
    coupons: dict = field(default_factory=dict)
    active: int = 0
    writing: int = 0
    retired: bool = False


class MobileRightsManager:
    def __init__(self, accounts, credentials, *, storage=None, cache=None, recover=None, accept_credentials=None,
                 connector=wap_session):
        self.accounts,self.credentials,self.storage = accounts,credentials,storage
        self.cache = cache or CredentialCache(storage)
        self.recover,self.accept_credentials,self.connector = recover,accept_credentials,connector
        self.lock,self.contexts,self.pending = RLock(),{},{}
        saved=storage.load_activity_data('mobile-rights') if storage else None
        if saved is not None and (not isinstance(saved,dict) or set(saved)!={'tracked','attempts'}
                                  or not isinstance(saved['tracked'],dict) or not isinstance(saved['attempts'],dict)):
            raise LoginStorageError('移动权益本机记录格式异常，已保留原数据。')
        self.data=saved or {'tracked':{},'attempts':{}}

    def _save(self):
        if self.storage:self.storage.save_activity_data('mobile-rights',self.data)

    def _snapshot(self, login_id):
        snapshot=self.credentials(login_id)
        if snapshot is None:
            raise MobileRightsError('账号已退出，请重新选择共享账号。','ACCOUNT_NOT_FOUND',404)
        return {**snapshot,'id':login_id}

    def _current(self, context):
        with self.lock:current=self.contexts.get(context.login_id) is context
        snapshot=self.credentials(context.login_id)
        return current and snapshot is not None and snapshot['phone']==context.phone

    def _context(self, login_id):
        snapshot=self._snapshot(login_id)
        with self.lock:
            current=self.contexts.get(login_id)
            if current is not None and current.phone==snapshot['phone']:
                return current
            future=self.pending.get(login_id)
            leader=future is None
            if leader:future=self.pending[login_id]=Future()
        if not leader:return future.result()
        session=None
        try:
            def recover(old):
                if self.recover is None:raise MobileRightsError('请先在账号管理恢复 APP 登录。','APP_LOGIN_REQUIRED',401,authentication=True)
                self.recover(old)
                latest=self._snapshot(login_id)
                if latest['phone']!=snapshot['phone']:raise MobileRightsError('账号身份已变化，请重新选择。','ACCOUNT_MISMATCH',409)
                return latest
            session=self.connector(snapshot,self.cache,recover=recover,accept_credentials=self.accept_credentials)
            context=RightsContext(login_id,snapshot['phone'],session)
            context.rights=RightsClient(session,context.phone,
                before_claim=lambda order:self._before_claim(login_id,order),
                finish_claim=lambda order,result:self._finish_claim(login_id,order,result),
                claimed=lambda order:self._claimed(login_id,order))
            context.youth=NewYouthClient(session)
            context.points=PointClaim(session,is_current=lambda:self._current(context))
            latest=self._snapshot(login_id)
            if latest['phone']!=context.phone:
                session.close();raise MobileRightsError('账号身份已变化，未继续使用原会话。','ACCOUNT_MISMATCH',409)
            with self.lock:self.contexts[login_id]=context
            future.set_result(context)
            return context
        except Exception as error:
            if session is not None:session.close()
            future.set_exception(error);raise
        finally:
            with self.lock:self.pending.pop(login_id,None)

    def _claimed(self,login_id,order):
        with self.lock:return deepcopy(self.data['attempts'].get(login_id,{}).get(order))

    def _before_claim(self,login_id,order):
        with self.lock:
            attempts=self.data['attempts'].setdefault(login_id,{})
            if order in attempts:raise MobileRightsError('此权益已提交领取，请先核对官方进度。','RIGHTS_ALREADY_SUBMITTED',409)
            attempts[order]={'status':'unknown','message':'领取请求已发出，请核对官方结果。'}
            self._save()

    def _finish_claim(self,login_id,order,result):
        with self.lock:
            attempts=self.data['attempts'].setdefault(login_id,{})
            if result is None:attempts.pop(order,None)
            else:attempts[order]=deepcopy(result)
            self._save()

    def busy_login(self,login_id):
        with self.lock:
            context=self.contexts.get(login_id)
            return login_id in self.pending or bool(context and context.writing)

    def forget(self,login_id):
        with self.lock:
            context=self.contexts.pop(login_id,None)
            if context:context.retired=True
            self.data['tracked'].pop(login_id,None);self.data['attempts'].pop(login_id,None);self._save()
        if context and not context.active:context.session.close()
        self.cache.forget(login_id,scope=SCOPE)

    def _retire(self,context):
        with self.lock:
            if self.contexts.get(context.login_id) is not context:return
            if context.writing or context.youth._selected is not None:
                raise MobileRightsError('该账号有正在进行的领取或活动验证码，本次未替换权益会话。','WAP_SESSION_BUSY',409)
            self.cache.invalidate(context.session)
            self.contexts.pop(context.login_id,None);context.retired=True
        if not context.active:context.session.close()

    def perform(self,login_id,kind,operation,*,write=False):
        for attempt in range(2):
            context=self._context(login_id)
            with self.lock:
                context.active+=1
                if write:context.writing+=1
            error=None
            try:
                result=operation(context)
                if not self._current(context):raise MobileRightsError('账号已退出或权益会话已更新，本次结果未应用。','WAP_CONTEXT_CHANGED',409)
                self.cache.sync(context.session)
                return result
            except (XQNError,PointClaimError) as source:
                status=source.code if source.code in (400,401,409,429,502,503) else 502
                if '频繁' in str(source):status=429
                message=str(source)+('；尝试更换服务ip再试。' if status==429 and '更换服务ip' not in str(source) else '')
                error=MobileRightsError(message,'WAP_LOGIN_REQUIRED' if status==401 else kind.upper()+'_FAILED',status,authentication=status==401,retry_after=getattr(source,'retry_after',None))
            except MobileRightsError as source:error=source
            except RequestException as source:error=MobileRightsError('连接移动权益服务失败，请稍后查询核对。','WAP_NETWORK')
            finally:
                with self.lock:
                    context.active-=1
                    if write:context.writing-=1
                    close=context.retired and context.active==0
                if close:context.session.close()
            if not write and not attempt and error.authentication:
                self._retire(context);continue
            raise error

    def query(self,login_id):
        with self.lock:tracked=list(self.data['tracked'].get(login_id,[]))
        return self.perform(login_id,'rights',lambda context:context.rights.query(tracked))

    def claim(self,login_id,action):
        return self.perform(login_id,'rights',lambda context:context.rights.claim(action),write=True)

    def youth_query(self,login_id):
        def operation(context):
            values=context.youth.getInfo_xqn()
            coupons={secrets.token_urlsafe(18):value for value in values}
            with self.lock:context.coupons=coupons
            return {'coupons':[{'id':key,'name':value['product_name']} for key,value in coupons.items()]}
        return self.perform(login_id,'youth',operation)

    def youth_sms(self,login_id,coupon_id):
        def operation(context):
            coupon=context.coupons.get(coupon_id)
            if coupon is None:raise MobileRightsError('请先查询并选择本账号的新青年兑换券。','YOUTH_COUPON_NOT_FOUND',409)
            return context.youth.send_sms(coupon['v_id'],coupon['coupon_id'])
        return self.perform(login_id,'youth',operation,write=True)

    def youth_claim(self,login_id,coupon_id,code):
        def operation(context):
            coupon=context.coupons.get(coupon_id)
            if coupon is None:raise MobileRightsError('所选兑换券已更新，请重新查询。','YOUTH_COUPON_NOT_FOUND',409)
            message=context.youth.lq_xqn(code,coupon['v_id'],coupon['coupon_id'])
            context.coupons.pop(coupon_id,None)
            return {'status':'success','message':message}
        return self.perform(login_id,'youth',operation,write=True)

    def point_query(self,login_id):
        return self.perform(login_id,'points',lambda context:context.points.get_info())

    def point_claim(self,login_id,claim_id):
        return self.perform(login_id,'points',lambda context:context.points.claim(claim_id),write=True)

    def track(self,login_id,name,enabled):
        self._snapshot(login_id)
        with self.lock:
            names=list(self.data['tracked'].get(login_id,[]))
            if enabled and name not in names:names.append(name)
            if not enabled:names=[value for value in names if value!=name]
            self.data['tracked'][login_id]=names;self._save()
            return {'tracked':names}


def register_mobile_rights(app,accounts,credentials,*,storage=None,cache=None,recover=None,accept_credentials=None):
    manager=MobileRightsManager(accounts,credentials,storage=storage,cache=cache,recover=recover,accept_credentials=accept_credentials)
    app.extensions['mobile_rights']=manager

    @app.get('/mobile-rights')
    def mobile_rights_page():return render_template('mobile_rights.html')

    @app.get('/api/mobile-rights/accounts')
    def mobile_rights_accounts():return jsonify(accounts=accounts())

    @app.post('/api/mobile-rights/<operation>')
    def mobile_rights_action(operation):
        payload=request.get_json(silent=True)
        operations={'query':('login_id',),'claim':('login_id','id'),'youth-query':('login_id',),'youth-sms':('login_id','id'),
                    'youth-claim':('login_id','id','code'),'points-query':('login_id',),'points-claim':('login_id','id'),'track':('login_id','name')}
        if operation not in operations:return jsonify(error='操作不存在。',code='NOT_FOUND'),404
        if not isinstance(payload,dict) or any(not isinstance(payload.get(key),str) or not payload[key].strip() for key in operations[operation]):
            return jsonify(error='请提供当前账号和有效操作参数。',code='INVALID_REQUEST'),400
        login_id=payload['login_id']
        if not any(account['id']==login_id for account in accounts()):return jsonify(error='请选择已有共享账号。',code='ACCOUNT_NOT_FOUND'),404
        if operation=='track' and (len(payload['name'])>512 or any(ord(c)<32 for c in payload['name']) or type(payload.get('enabled')) is not bool):
            return jsonify(error='请填写有效权益名称和记录状态。',code='INVALID_REQUEST'),400
        try:
            if operation=='query':result=manager.query(login_id)
            elif operation=='claim':result=manager.claim(login_id,payload['id'])
            elif operation=='youth-query':result=manager.youth_query(login_id)
            elif operation=='youth-sms':result=manager.youth_sms(login_id,payload['id'])
            elif operation=='youth-claim':result=manager.youth_claim(login_id,payload['id'],payload['code'])
            elif operation=='points-query':result=manager.point_query(login_id)
            elif operation=='points-claim':result=manager.point_claim(login_id,payload['id'])
            else:result=manager.track(login_id,payload['name'].strip(),payload['enabled'])
            return jsonify(result)
        except MobileRightsError as error:
            message=re.sub(r'(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)',r'\1****\2',str(error))
            return jsonify(error=message,code=error.code,retry_after=error.retry_after),error.status
