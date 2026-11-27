"""Account-scoped partner session checks, QR renewal and validated local import."""
from copy import deepcopy
from contextlib import contextmanager
import json
import re
import secrets
from threading import Lock,RLock
import time

from flask import Blueprint,jsonify,request,session

from workspace.accounts.credentials import CredentialCache
from workspace.http_transport import requests
from .baidu_map import CheckinBaiduMap,SCOPE as BAIDU_SCOPE,HOME,ENTRY as BAIDU_ENTRY,USER_AGENT
from .taobao import CheckinTaobao,SCOPE as TAOBAO_SCOPE,ENTRY as TAOBAO_ENTRY
from .kuaishou import CheckinKuaishou, SCOPE as KUAISHOU_SCOPE, LOGIN_FIELDS as KUAISHOU_FIELDS, normalize_login, QUERY as KUAISHOU_QUERY
from .partner_qr import PartnerQr
from .client import CheckinError

SERVICES={'baidu-map':{'title':'百度地图','scope':BAIDU_SCOPE,'expiry':('BDUSS','BDUSS_BFESS'),'domains':('baidu.com',)},
          'taobao':{'title':'淘宝 / 菜鸟','scope':TAOBAO_SCOPE,'expiry':('cookie2','sgcookie'),'domains':('taobao.com','tmall.com')},
          'kuaishou':{'title':'快手极速版','scope':KUAISHOU_SCOPE,'expiry':(),'domains':('kuaishou.com',)}}
PROFILE_KEY='partner-login-profiles'
FLOW_RETENTION_SECONDS=900  # Local unfinished-dialog cleanup, not partner credential validity.


def parse_import(text,service):
    if not isinstance(text,str) or not text.strip() or len(text)>60000:
        raise CheckinError('请提供当前合作方的 Cookie 或 JSON 登录资料。','INVALID_IMPORT',400)
    text=text.strip();metadata={}
    if text.startswith(('{','[')):
        try:value=json.loads(text)
        except ValueError as error:raise CheckinError('JSON 格式不正确。','INVALID_IMPORT',400) from error
        if isinstance(value,dict):
            keys=KUAISHOU_FIELDS if service=='kuaishou' else ('uid','phone_info')
            metadata={k:value[k] for k in keys if k in value}
            cookies=value.get('cookies')
            if service=='kuaishou':cookies=value.get('cookies',[])
        else:cookies=value
        if not isinstance(cookies,list):raise CheckinError('JSON 必须包含 cookies 数组。','INVALID_IMPORT',400)
    elif '\t' in text:
        cookies=[]
        for line in text.splitlines():
            line=line.removeprefix('#HttpOnly_')
            if not line or line.startswith('#'):continue
            parts=line.split('\t')
            if len(parts)!=7:raise CheckinError('Netscape Cookie 文件格式不正确。','INVALID_IMPORT',400)
            domain,_flag,path,secure,expires,name,value=parts
            cookies.append({'domain':domain,'path':path,'secure':secure.upper()=='TRUE','expires':expires,'name':name,'value':value})
    else:
        if text.lower().startswith('cookie:'):text=text[7:].strip()
        cookies=[]
        for part in text.split(';'):
            name,separator,value=part.strip().partition('=')
            if not separator:raise CheckinError('Cookie 应为名称=值，多个项目以分号分隔。','INVALID_IMPORT',400)
            cookies.append({'name':name,'value':value,'domain':'.'+SERVICES[service]['domains'][0],'path':'/','secure':True})
    normalized=[]
    for cookie in cookies:
        if not isinstance(cookie,dict):raise CheckinError('Cookie 项目格式不正确。','INVALID_IMPORT',400)
        name,value=cookie.get('name'),cookie.get('value');domain=str(cookie.get('domain') or '.'+SERVICES[service]['domains'][0]).lower()
        if (not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+',name) or not isinstance(value,str)
                or any(c in value for c in '\r\n\0') or not any(domain.lstrip('.')==d or domain.endswith('.'+d) for d in SERVICES[service]['domains'])):
            raise CheckinError('资料包含无效 Cookie 或其他站点的数据，请仅导入当前合作方登录资料。','INVALID_IMPORT',400)
        expires=cookie.get('expires',cookie.get('expirationDate'))
        try:expires=int(float(expires)) if expires is not None else None
        except (ValueError,TypeError,OverflowError):raise CheckinError('Cookie 到期时间必须使用 Unix 秒。','INVALID_IMPORT',400)
        if expires is not None and expires<=0:expires=None
        path=cookie.get('path') or '/'
        if not isinstance(path,str) or not path.startswith('/'):raise CheckinError('Cookie 路径格式不正确。','INVALID_IMPORT',400)
        normalized.append({'name':name,'value':value,'domain':domain,'path':path,'secure':bool(cookie.get('secure',True)),'expires':expires})
    return normalized,metadata


class PartnerLogins:
    def __init__(self,accounts,credentials,cache,beans,*,qr_factory=PartnerQr,now=time.time):
        self.accounts,self.credentials,self.cache,self.beans=accounts,credentials,cache,beans
        self.storage=cache.storage;self.qr_factory,self.now=qr_factory,now
        self.lock=RLock();self.flows={}
        self.profiles=(self.storage.load_activity_data(PROFILE_KEY) if self.storage else None) or {}
        for account in accounts():
            snapshot=self.snapshot(account['id'])
            for service in SERVICES:
                saved=cache.peek(SERVICES[service]['scope'],snapshot)
                if saved and any(k in saved['data'] for k in ('uid','phone_info')) and account['id']+':'+service not in self.profiles:
                    self._record(account['id'],service,{k:saved['data'][k] for k in ('uid','phone_info') if k in saved['data']})
        beans.login_maintenance=self.prune

    def snapshot(self,login_id):
        if not isinstance(login_id,str):raise CheckinError('请选择当前共享账号。','ACCOUNT_MISSING',400)
        value=self.credentials(login_id)
        if value is None:raise CheckinError('该账号已退出。','ACCOUNT_MISSING',404)
        return {**value,'id':login_id}

    def context(self,login_id,service):
        if not isinstance(service,str) or service not in SERVICES:raise CheckinError('请选择支持的合作方。','INVALID_SERVICE',400)
        snapshot=self.snapshot(login_id);saved=self.cache.peek(SERVICES[service]['scope'],snapshot)
        with self.lock:profile=deepcopy(self.profiles.get(login_id+':'+service,{}))
        keys=KUAISHOU_FIELDS if service=='kuaishou' else ('uid','phone_info')
        metadata={k:saved['data'][k] for k in keys if k in saved['data']} if saved else {}
        metadata.update({k:profile[k] for k in ('uid','phone_info') if k in profile})
        return snapshot,saved,metadata

    def _record(self,login_id,service,values):
        with self.lock:
            updated=deepcopy(self.profiles);updated.setdefault(login_id+':'+service,{}).update(values)
            if self.storage:self.storage.save_activity_data(PROFILE_KEY,updated)
            self.profiles=updated

    def status(self,login_id):
        snapshot=self.snapshot(login_id);rows=[]
        for service,config in SERVICES.items():
            saved=self.cache.peek(config['scope'],snapshot)
            with self.lock:profile=deepcopy(self.profiles.get(login_id+':'+service,{}))
            active=bool(saved and (saved['expires_at'] is None or saved['expires_at']>self.now()))
            state='saved' if active else 'needs_update'
            if profile.get('error_code')=='VERIFICATION_REQUIRED':state='verification_required'
            message=profile.get('message') or '已保存，执行时按官方有效期复用。'
            if saved and not active:message='官方记录的登录期限已到，请扫码或导入新资料。'
            elif not saved and not profile.get('error_code'):
                message='当前没有可用登录态，请扫码或手动更新。' if profile else '尚未保存登录态。'
            rows.append({'service':service,'title':config['title'],'state':state,
                'expires_at':saved['expires_at'] if saved else None,'checked_at':profile.get('checked_at'),
                'uid_hint':'****'+str(profile['uid'])[-4:] if profile.get('uid') else '',
                'message':message,
                'qr_available':service=='taobao' or service=='baidu-map' and bool(profile.get('phone_info') or saved and saved['data'].get('phone_info'))})
        return {'login_id':login_id,'services':rows,'data_directory':str(self.storage.path.parent) if self.storage else None}

    @contextmanager
    def candidate(self,snapshot,service,cookies,metadata):
        headers={'User-Agent':USER_AGENT,'Referer':BAIDU_ENTRY} if service=='baidu-map' else {
            'User-Agent':'Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 Mobile Safari/537.36','Referer':TAOBAO_ENTRY,'Origin':'https://pages-fast.m.taobao.com'}
        if service=='kuaishou':headers={'User-Agent':'kwai-android'}
        with requests.Session(impersonate='chrome',headers=headers) as client:
            for cookie in cookies:
                client.cookies.set(cookie['name'],cookie['value'],domain=cookie['domain'],path=cookie['path'])
                for value in client.cookies.jar:
                    if (value.name,value.domain,value.path)==(cookie['name'],cookie['domain'],cookie['path']):
                        value.expires=cookie.get('expires');value.secure=cookie.get('secure',True)
            temporary=CredentialCache(now=self.now)
            temporary.connect(SERVICES[service]['scope'],snapshot,client,lambda:metadata,expiry_cookies=SERVICES[service]['expiry'])
            yield client,temporary

    def validate(self,snapshot,service,client,temporary,metadata):
        if service=='baidu-map':
            info=metadata.get('phone_info')
            if not isinstance(info,dict) or not isinstance(info.get('cuid'),str) or not info['cuid'] or not isinstance(info.get('sv'),str):
                raise CheckinError('首次配置百度地图需在 JSON 中提供本人的 phone_info（cuid、sv）；后续扫码更新会复用这些资料。','BAIDU_DEVICE_MISSING',400)
            info={key:info[key] for key in ('cuid','sv')}
            result=CheckinBaiduMap(lambda:snapshot,temporary)._request(client,HOME,{**info,'task_from':'zgyd','from':'ChinaMobiletask','version':2,'activity':'goldcoinshop'})
            if result.get('errno')!=0:raise CheckinError('百度地图未返回有效的本人资料，原登录态已保留。','PARTNER_PROFILE_REJECTED',409)
            data=result.get('data')
            if not isinstance(data,dict) or 'is_login' not in data:
                raise CheckinError('百度地图身份响应不完整，原登录态已保留。','PARTNER_PROFILE_REJECTED',409)
            if str(data['is_login'])!='1':raise CheckinError('百度地图登录态已失效。','PARTNER_LOGIN_REQUIRED',401)
            uid=str(data.get('uid') or '')
            if not uid:raise CheckinError('百度地图未确认本人身份，原登录态已保留。','PARTNER_PROFILE_REJECTED',409)
            result_meta={'uid':uid,'phone_info':info}
        elif service=='kuaishou':
            result_meta=normalize_login(metadata)
            client._credential_data=result_meta
            CheckinKuaishou(lambda:snapshot,temporary)._request(client,KUAISHOU_QUERY,{'shareText':'','deepLink':''})
            uid=result_meta['uid']
        else:
            result=CheckinTaobao(lambda:snapshot,temporary)._call(client,'mtop.user.getUserSimple',{})
            uid=str(result.get('userNumId') or result.get('userId') or '') if isinstance(result,dict) else ''
            if not uid:raise CheckinError('淘宝未确认本人身份，原登录态已保留。','PARTNER_PROFILE_REJECTED',409)
            result_meta={'uid':uid}
        if metadata.get('uid') and str(metadata['uid'])!=uid:
            raise CheckinError('扫码或导入的合作方账号与原绑定不一致，请使用原账号。','ACCOUNT_MISMATCH',409)
        for cookie in list(client.cookies.jar):
            if cookie.expires is not None and cookie.expires<=self.now():client.cookies.jar.clear(cookie.domain,cookie.path,cookie.name)
        return result_meta

    def commit(self,snapshot,service,client,metadata,previous,*,replace=True):
        latest=self.snapshot(snapshot['id'])
        if latest['phone']!=snapshot['phone']:raise CheckinError('当前账号已变化，请重新操作。','ACCOUNT_MISMATCH',409)
        saved=self.cache.install(SERVICES[service]['scope'],latest,client,metadata,expiry_cookies=SERVICES[service]['expiry'],
            expected_generation=previous['generation'] if previous else None,replace_identity=replace)
        if saved is None:raise CheckinError('登录态已被另一项更新替换，请重新检查。','PARTNER_LOGIN_CHANGED',409)
        profile={key:metadata[key] for key in ('uid','phone_info') if key in metadata}
        self._record(snapshot['id'],service,{**profile,'checked_at':self.now(),'message':'已核对官方身份并保存登录态。','error_code':''})

    def _begin(self,login_id):
        if not self.beans.begin_login_update(login_id):
            raise CheckinError('该账号有任务、兑换或登录更新正在进行，请完成后再更新。','ACCOUNT_BUSY',409)

    def bind_legacy_identity(self,snapshot,service,saved,metadata):
        if saved and not metadata.get('uid'):
            try:
                with self.candidate(snapshot,service,saved['cookies'],metadata) as (client,temporary):
                    verified=self.validate(snapshot,service,client,temporary,metadata)
                    self.commit(snapshot,service,client,verified,saved,replace=False)
                    return self.cache.peek(SERVICES[service]['scope'],snapshot),verified
            except CheckinError as error:
                if error.status!=401:raise
        return saved,metadata

    def check(self,login_id,service):
        snapshot,saved,metadata=self.context(login_id,service)
        if saved is None:raise CheckinError('尚无可检查的登录态，请扫码或手动更新。','PARTNER_LOGIN_REQUIRED',401)
        self._begin(login_id)
        try:
            with self.candidate(snapshot,service,saved['cookies'],metadata) as (client,temporary):
                verified=self.validate(snapshot,service,client,temporary,metadata)
                self.commit(snapshot,service,client,verified,saved,replace=False)
        except CheckinError as error:
            self._record(login_id,service,{'checked_at':self.now(),'message':str(error),'error_code':error.code})
            if error.status==401:self.cache.discard(SERVICES[service]['scope'],snapshot,expected_generation=saved['generation'])
            raise
        finally:self.beans.end_login_update(login_id)
        self.beans.tick(restored={login_id})
        return self.status(login_id)

    def import_session(self,login_id,service,text):
        snapshot,saved,metadata=self.context(login_id,service);cookies,extra=parse_import(text,service)
        self._begin(login_id)
        try:
            saved,metadata=self.bind_legacy_identity(snapshot,service,saved,metadata)
            if 'uid' in extra and metadata.get('uid') and str(extra['uid'])!=str(metadata['uid']):
                raise CheckinError('导入资料的账号与当前绑定不一致。','ACCOUNT_MISMATCH',409)
            metadata.update(extra)
            with self.candidate(snapshot,service,cookies,metadata) as (client,temporary):
                verified=self.validate(snapshot,service,client,temporary,metadata)
                self.commit(snapshot,service,client,verified,saved)
        finally:self.beans.end_login_update(login_id)
        self.beans.tick(restored={login_id})
        return self.status(login_id)

    def start_qr(self,owner,login_id,service):
        self.prune();snapshot,saved,metadata=self.context(login_id,service)
        if service=='kuaishou':
            raise CheckinError('快手当前支持导入本人完整 JSON 登录资料，请使用手动更新。','QR_UNAVAILABLE',409)
        if service=='baidu-map' and not metadata.get('phone_info'):
            raise CheckinError('首次配置百度地图需先导入本人设备资料，后续可直接扫码更新。','BAIDU_DEVICE_MISSING',400)
        self._begin(login_id);qr=None
        try:
            saved,metadata=self.bind_legacy_identity(snapshot,service,saved,metadata)
            qr=self.qr_factory('baidu' if service=='baidu-map' else service)
            image=qr.start()
        except Exception:
            if qr is not None:qr.close()
            self.beans.end_login_update(login_id);raise
        flow_id=secrets.token_urlsafe(24)
        with self.lock:self.flows[flow_id]={'owner':owner,'id':login_id,'service':service,'snapshot':snapshot,'previous':saved,
            'metadata':metadata,'qr':qr,'lock':Lock(),'deadline':self.now()+FLOW_RETENTION_SECONDS,'cancelled':False,'state':'waiting','image':image}
        return {'flow_id':flow_id,'login_id':login_id,'service':service,'state':'waiting','image':image,'message':'请使用官方 APP 扫码并确认；不需要 USB 或 root 连接。'}

    def flow(self,owner,flow_id):
        with self.lock:flow=self.flows.get(flow_id)
        if not flow or flow['owner']!=owner:raise CheckinError('本次扫码已结束，请重新获取二维码。','QR_MISSING',404)
        return flow

    def poll(self,owner,flow_id):
        flow=self.flow(owner,flow_id)
        if not flow['lock'].acquire(blocking=False):return {'state':'waiting','login_id':flow['id'],'message':'正在查询官方确认…'}
        try:
            if flow['cancelled'] or self.now()>flow['deadline']:
                self.cancel(owner,flow_id)
                return {'state':'closed','login_id':flow['id'],'message':'本机扫码会话已结束，请重新获取。'}
            state=flow['qr'].poll();flow['state']=state
            if state=='confirmed':
                cookies=[{'name':c.name,'value':c.value,'domain':c.domain,'path':c.path,'secure':c.secure,'expires':c.expires} for c in flow['qr'].client.cookies.jar]
                with self.candidate(flow['snapshot'],flow['service'],cookies,flow['metadata']) as (client,temporary):
                    metadata=self.validate(flow['snapshot'],flow['service'],client,temporary,flow['metadata'])
                    if flow['cancelled']:return {'state':'expired','login_id':flow['id']}
                    self.commit(flow['snapshot'],flow['service'],client,metadata,flow['previous'])
                flow['updated']=True
                self.cancel(owner,flow_id)
                return {'state':'complete','login_id':flow['id'],'message':'登录身份已验证，新的登录态已加密保存。','status':self.status(flow['id'])}
            if state=='expired':self.cancel(owner,flow_id)
            return {'state':state,'login_id':flow['id'],'message':'已扫码，请在官方 APP 内确认。' if state=='scanned' else '等待扫码确认。' if state=='waiting' else '二维码已过期，请重新获取。'}
        except CheckinError:
            self.cancel(owner,flow_id);raise
        finally:
            if flow['cancelled']:
                flow['qr'].close();self.beans.end_login_update(flow['id'])
                if flow.get('updated'):self.beans.tick(restored={flow['id']})
            flow['lock'].release()

    def cancel(self,owner,flow_id):
        with self.lock:
            flow=self.flows.get(flow_id)
            if not flow or flow['owner']!=owner:return
            self.flows.pop(flow_id);flow['cancelled']=True
        if flow['lock'].acquire(blocking=False):
            try:flow['qr'].close();self.beans.end_login_update(flow['id'])
            finally:flow['lock'].release()

    def prune(self):
        with self.lock:expired=[(id,f['owner']) for id,f in self.flows.items() if f['deadline']<self.now()]
        for id,owner in expired:self.cancel(owner,id)


def register_partner_logins(app,accounts,credentials,cache,beans):
    manager=PartnerLogins(accounts,credentials,cache,beans)
    blueprint=Blueprint('partner_logins',__name__)
    @blueprint.errorhandler(CheckinError)
    def error(error):return jsonify(error=str(error),code=error.code),error.status
    @blueprint.get('/api/checkin/partners')
    def status():return jsonify(manager.status(request.args.get('login_id')))
    @blueprint.post('/api/checkin/partners/<operation>')
    def operate(operation):
        payload=request.get_json(silent=True)
        if not isinstance(payload,dict):raise CheckinError('请提供更新参数。','INVALID_OPERATION',400)
        if operation=='check':return jsonify(manager.check(payload.get('login_id'),payload.get('service')))
        if operation=='import':return jsonify(manager.import_session(payload.get('login_id'),payload.get('service'),payload.get('text')))
        owner=session.setdefault('partner_login_sid',secrets.token_hex(16))
        if operation=='qr-start':return jsonify(manager.start_qr(owner,payload.get('login_id'),payload.get('service')))
        flow_id=payload.get('flow_id')
        if not isinstance(flow_id,str):raise CheckinError('请选择本次二维码。','QR_MISSING',400)
        if operation=='qr-poll':return jsonify(manager.poll(owner,flow_id))
        if operation=='qr-cancel':manager.cancel(owner,flow_id);return jsonify(cancelled=True)
        raise CheckinError('不支持的登录操作。','INVALID_OPERATION',400)
    app.register_blueprint(blueprint);app.extensions['partner_logins']=manager
    app.config['PARTNER_LOGINS_ENABLED']=True
    return manager
