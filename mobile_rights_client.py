"""Current-month WAP orders. Query and submission remain separate operations."""
from datetime import datetime
import random
import re
import secrets
from threading import Lock

from http_transport import RequestException
from mobile_rights_auth import SCOPE, ENTRY, MobileRightsError, cookie_header, response_json
from recharge.cmcc import SHANGHAI

STATES = {'0':'已发放','1':'已完成','2':'已失效','4':'处理中','5':'待领取'}


class RightsClient:
    def __init__(self, session, phone, *, before_claim, finish_claim, claimed, now=None):
        self.session,self.phone = session,phone
        self.before_claim,self.finish_claim,self.claimed = before_claim,finish_claim,claimed
        self.now = now or (lambda:datetime.now(SHANGHAI))
        self.lock,self.orders = Lock(),{}

    def query(self, tracked=()):
        if not self.lock.acquire(blocking=False):
            raise MobileRightsError('权益查询或领取正在进行，请稍候。','RIGHTS_BUSY',409)
        try:
            self.orders={}
            nonce=f'{random.random():.16f}'
            items,orders,seen,warnings = [],{},set(),[]
            month=self.now().strftime('%Y-%m');complete=False
            for page in range(1,31):
                try:
                    response=self.session.get(SCOPE+'/rightOrder/queryOrderList',
                        params={'status':'','pageIndex':str(page),'ajaxSubmitType':'post','ajax_randomcode':nonce},
                        headers={'Referer':ENTRY,'Cookie':cookie_header(self.session,nonce,SCOPE+'/rightOrder/queryOrderList')},timeout=(10,25),allow_redirects=False)
                    data=response_json(response)
                except RequestException as error:
                    raise MobileRightsError('移动权益查询连接中断，请稍后重新查询。','RIGHTS_NETWORK') from error
                body=data.get('data')
                if not isinstance(body,dict) or not isinstance(body.get('list'),list):
                    raise MobileRightsError('官方未返回可识别的权益列表。','RIGHTS_FORMAT')
                rows=body['list'];older=False
                if not rows:
                    complete=True;break
                for row in rows:
                    if not isinstance(row,dict) or not isinstance(row.get('rightName'),str) or not isinstance(row.get('orderid'),str):
                        raise MobileRightsError('官方权益条目缺少名称或订单标识。','RIGHTS_FORMAT')
                    order_id=row['orderid'];number=str(row.get('userPhone') or self.phone)
                    if number != self.phone:
                        raise MobileRightsError('权益订单号码与当前账号不一致，已停止操作。','ACCOUNT_MISMATCH',409)
                    try:moment=datetime.fromtimestamp(float(row['orderTime'])/1000,SHANGHAI)
                    except (KeyError,TypeError,ValueError,OverflowError) as error:
                        raise MobileRightsError('官方权益订单时间无法识别。','RIGHTS_FORMAT') from error
                    period=moment.strftime('%Y-%m')
                    if period < month:older=True;continue
                    if period != month or order_id in seen:continue
                    seen.add(order_id)
                    status=str(row.get('status',''))
                    action=secrets.token_urlsafe(18)
                    prior=self.claimed(order_id)
                    available=status=='5' and prior is None
                    item={'id':action,'order_no':order_id,'name':row['rightName'],
                          'order_time':moment.isoformat(),'phone_masked':self.phone[:3]+'****'+self.phone[-4:],
                          'status_code':status,'status':STATES.get(status,'官方状态 '+status),
                          'can_claim':available,'tracked':row['rightName'] in tracked,'note':''}
                    if prior and status=='5':
                        item.update(status='领取成功，等待状态更新' if prior.get('status')=='success' else '领取结果待核实',note=prior.get('message') or '已提交，请核对官方进度，不会重复领取。')
                    if available:orders[action]=order_id
                    items.append(item)
                if older or body.get('hasNext') is False:
                    complete=True;break
            if not complete:warnings.append('已查询前30页，结果尚未查全；未自动继续请求。')
            names={item['name'] for item in items}
            for name in tracked:
                if name not in names:
                    items.append({'id':None,'order_no':'','name':name,'order_time':None,'phone_masked':self.phone[:3]+'****'+self.phone[-4:],
                                  'status_code':'','status':'本月未发放' if complete else '待核对','can_claim':False,'tracked':True,'note':''})
            self.orders=orders
            return {'items':items,'month':month,'warnings':warnings,'complete':complete,'queried_at':self.now().isoformat()}
        finally:self.lock.release()

    def claim(self, action):
        if not self.lock.acquire(blocking=False):
            raise MobileRightsError('权益查询或领取正在进行，请稍候。','RIGHTS_BUSY',409)
        try:
            order_id=self.orders.pop(action,None)
            if order_id is None:
                raise MobileRightsError('请先查询本账号权益，再领取列表中待领取的订单。','RIGHTS_NOT_CLAIMABLE',409)
            self.before_claim(order_id)
            nonce=f'{random.random():.16f}'
            try:
                response=self.session.get(SCOPE+'/rightOrder/rightReceive',params={'orderId':order_id,'ajax_randomcode':nonce},
                    headers={'Referer':ENTRY,'Cookie':cookie_header(self.session,nonce,SCOPE+'/rightOrder/rightReceive')},timeout=(10,25),allow_redirects=False)
                data=response_json(response)
            except (RequestException,MobileRightsError) as error:
                if isinstance(error,MobileRightsError) and (error.authentication or error.status==429):
                    self.finish_claim(order_id,None)
                    raise
                self.finish_claim(order_id,{'status':'unknown','message':'领取结果未确认，请查询官方进度；本程序不会重复提交。'})
                raise MobileRightsError('领取结果未确认，请重新查询官方进度，不会自动重复提交。','RIGHTS_RESULT_UNKNOWN') from error
            message=str(data.get('X_RESULTINFO') or '')
            if str(data.get('X_RESULTCODE'))=='0' and message.strip().upper() in ('SUCCESS','成功','领取成功'):
                result={'status':'success','message':'权益领取成功。'}
                self.finish_claim(order_id,result);return result
            if str(data.get('X_RESULTCODE')) not in ('','0'):
                self.finish_claim(order_id,None)
                raise MobileRightsError(message or '官方未允许领取该权益。','RIGHTS_REJECTED',400)
            result={'status':'unknown','message':'官方未返回明确的领取结果，请重新查询核对；不会重复提交。'}
            self.finish_claim(order_id,result);return result
        finally:self.lock.release()
