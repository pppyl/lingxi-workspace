"""Official wallet notices, using existing wallet sessions without APP login."""
from copy import deepcopy
import base64
from html.parser import HTMLParser
import re
from threading import RLock
import time

from http_transport import requests, RequestException
from coupon_client import APP_UA, ENTRY, ORIGIN, CouponClient, CouponError, _unpack, _date

SCOPE='coupon-notices'
INTERVAL=300


class NoticeText(HTMLParser):
    def __init__(self):
        super().__init__();self.parts=[];self.skip=0

    def handle_starttag(self, tag, attrs):
        if tag in ('script','style'):self.skip+=1
        if tag in ('p','div','br','li','h1','h2','h3'):self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script','style'):self.skip=max(0,self.skip-1)
        if tag in ('p','div','li'):self.parts.append('\n')

    def handle_data(self, value):
        if not self.skip:self.parts.append(value)


def notice_text(value):
    parser=NoticeText();parser.feed(str(value or ''))
    return '\n'.join(line.strip() for line in ''.join(parser.parts).replace('\xa0',' ').splitlines() if line.strip())


class CouponNotices:
    def __init__(self, snapshots, cache, storage=None, *, now=time.time, session_factory=None):
        self.snapshots,self.cache,self.storage=snapshots,cache,storage
        self.now,self.session_factory=now,session_factory or (lambda:requests.Session(impersonate='chrome'))
        self.lock=RLock()
        self.data=(storage.load_activity_data(SCOPE) if storage else None) or {}

    def get(self):
        with self.lock:
            now=self.now()
            materials=self.snapshots()
            provinces=sorted({str(row.get('profile',{}).get('provinceCode')) for row in materials
                              if re.fullmatch(r'\d{2,4}',str(row.get('profile',{}).get('provinceCode','')))})
            for province in provinces:
                saved=self.data.setdefault(province,{'notices':[],'checked_at':0,'next_check_at':0,'error':''})
                if now<saved['next_check_at']:continue
                selected=None
                for snapshot in materials:
                    if str(snapshot.get('profile',{}).get('provinceCode'))!=province:continue
                    cached=self.cache.load(SCOPE,snapshot)
                    if cached is None:
                        wallet=self.cache.load('coupon-wallet:12014',snapshot)
                        if wallet is not None:
                            cached=deepcopy(wallet)
                            if self.cache.storage:self.cache.storage.save_credentials(SCOPE,self.cache.account(snapshot),cached)
                            with self.cache.lock:self.cache.entries[SCOPE,self.cache.account(snapshot)]=cached
                    if cached is not None:
                        selected=snapshot;break
                if selected is None:
                    saved['error']='暂无可复用的卡券会话，公告暂未同步；不会为读取公告额外登录。'
                    continue
                client=self.session_factory()
                client.headers.update({'User-Agent':APP_UA,'Referer':ENTRY,'Origin':ORIGIN})
                try:
                    def unavailable():
                        raise CouponError('公告会话已失效，等待卡券会话恢复后同步。','NOTICE_SESSION_MISSING')
                    self.cache.connect(SCOPE,selected,client,unavailable)
                    def read(body):
                        # ownAjax GET encodes each business field, unlike the wallet POST envelope.
                        params={key:base64.b64encode(str(value).encode()).decode() for key,value in body.items()}
                        params['time']=str(int(self.now()*1000))
                        response=client.get(ORIGIN+'/i/v1/cust/noticeinfo/'+province,
                            params=params,timeout=(10,20),allow_redirects=False)
                        return _unpack(CouponClient._json(response),allow_list=True)
                    rows=read({'channelCode':'0003','placeCode':''})
                    if not isinstance(rows,list):raise CouponError('官方公告列表格式异常。','NOTICE_FORMAT')
                    notices=[]
                    for row in rows:
                        end=_date(row.get('end_time'))
                        if end and self._timestamp(end)<=now:continue
                        detail=read({'provice':province,'id':row['id'],'channelCode':'0003'})
                        if not isinstance(detail,dict):raise CouponError('官方公告详情格式异常。','NOTICE_FORMAT')
                        notices.append({'id':str(row['id']),'title':notice_text(detail.get('title') or row.get('title')),
                            'content':notice_text(detail.get('content')),'start_at':_date(row.get('start_time')),
                            'end_at':end,'published_at':_date(row.get('operate_time'))})
                    saved.update(notices=notices,checked_at=now,error='')
                except (CouponError,RequestException,ValueError,KeyError,TypeError) as error:
                    if isinstance(error,CouponError) and error.code=='COUPON_LOGIN_REQUIRED' and getattr(client,'_credential_binding',None):
                        self.cache.invalidate(client)
                    saved['error']='官方公告暂未同步，保留上次读取的公告；请稍后刷新。'
                finally:
                    client.close();saved['next_check_at']=self.now()+INTERVAL
                if self.storage:self.storage.save_activity_data(SCOPE,self.data)
            notices={}
            checked=[];errors=[]
            for saved in self.data.values():
                if saved['checked_at']:checked.append(saved['checked_at'])
                if saved['error']:errors.append(saved['error'])
                for row in saved['notices']:
                    if row['end_at'] and self._timestamp(row['end_at'])<=now:continue
                    if row['start_at'] and self._timestamp(row['start_at'])>now:continue
                    notices.setdefault(row['id'],deepcopy(row))
            return {'notices':list(notices.values()),'checked_at':min(checked) if checked else None,
                    'stale':bool(errors),'message':next(iter(errors),'')}

    @staticmethod
    def _timestamp(value):
        from datetime import datetime
        return datetime.fromisoformat(value).timestamp()
