"""Global Pass Star Day protocol, verified against the current official H5.

Evidence and source hashes: analysis/star-day-protocol/. All business sessions
are account-local; only the existing APP login is shared between features.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import math
import hashlib
import logging
from queue import SimpleQueue
import re
import time

from http_transport import requests
from checkin_client import CheckinError
from checkin_flash import run_flash_burst
from recharge.cmcc import SHANGHAI
from star_day_auth import star_day_session, StarDayAuthError, explain_rate_limit

ORIGIN = 'https://qqt.cmicrwx.cn'
BASE = ORIGIN+'/2016tyjf/xhmqqthy'
ENTRY = BASE+'/res/wap/xdrNormal.html?sspId=75628'
TIERS = {'0': '普卡', '1': '银卡', '2': '金卡', '3': '白金卡', '4': '钻石卡', '8': '卓越钻石卡'}


class StarDayError(CheckinError):
    def __init__(self, message, code='STAR_DAY_ERROR', status=502, *, uncertain=False, rejected=False):
        super().__init__(message, code, status, uncertain=uncertain)
        self.rejected = rejected


class StarDayClient:
    def __init__(self, credentials_provider, *, session_factory=None, worker_session_factory=None, session_cache=None, allow_login=True, prepare_time='09:30'):
        self.credentials_provider = credentials_provider
        self.session_factory = session_factory
        self.session_cache = session_cache
        self.allow_login, self.prepare_time = allow_login, prepare_time
        self.worker_session_factory = worker_session_factory or (lambda: requests.Session(impersonate='chrome', default_headers=False, use_thread_local_curl=False))
        self.clock = None

    @contextmanager
    def _session(self, *, force=False, allow_login=None):
        try:
            with star_day_session(self.credentials_provider, session_factory=self.session_factory,
                                  cache=self.session_cache, force=force,
                                  allow_login=self.allow_login if allow_login is None else allow_login) as session:
                yield session
        except StarDayAuthError as error:
            raise StarDayError(str(error), error.code, error.status) from error
        except StarDayError as error:
            # Only an explicit upstream logout invalidates a cached campaign
            # cookie. Business rejection or rate limiting does not log it out.
            if self.session_cache is not None and error.code == '-10':
                snapshot = self.credentials_provider()
                self.session_cache.forget(snapshot.get('id') or snapshot['phone'])
            raise

    def prepare(self):
        snapshot = self.credentials_provider()
        saved = self.session_cache.load(snapshot.get('id') or snapshot['phone'], snapshot['phone']) if self.session_cache else None
        now = datetime.fromtimestamp(self.session_cache.now(), SHANGHAI) if self.session_cache else datetime.now(SHANGHAI)
        hour, minute = map(int, self.prepare_time.split(':'))
        cutoff = now.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()
        if saved and saved['created_at'] >= cutoff:
            return {'state':'ready', 'message':f'当前凭据在 {self.prepare_time} 后建立，直接用于抢购，无需重复登录。'}
        if saved and saved.get('source') == 'sms' and saved['expires_at'] > now.replace(hour=10,minute=0,second=30,microsecond=0).timestamp():
            return {'state':'ready', 'message':'当前短信登录凭据可覆盖本次抢购，继续复用。'}
        with self._session(force=saved is not None, allow_login=True):
            return {'state':'ready', 'message':'抢购凭据已准备，有效期最长 2 小时。'}

    def _current(self, snapshot):
        latest = self.credentials_provider()
        cached = self.session_cache.load(snapshot.get('id') or snapshot['phone'],snapshot['phone']) if self.session_cache else None
        if latest is None or latest['phone'] != snapshot['phone'] or not cached and latest['cookie'] != snapshot['cookie']:
            raise StarDayError('共享账号登录已变更，请使用最新登录态重试。', 'APP_CREDENTIALS_CHANGED', 409)

    def _request(self, client, path, *, data=None, json=None, writing=False):
        try:
            response = client.post(BASE+path, data=data, json=json,
                headers={'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'} if data is not None else {},
                timeout=(10, 20), allow_redirects=False) if data is not None or json is not None else client.get(BASE+path, timeout=(10, 20), allow_redirects=False)
        except requests.RequestsError as error:
            raise StarDayError('请求中断，提交结果待核实。' if writing else '连接星动日服务失败，请稍后重试。',
                'RESULT_UNCERTAIN' if writing else 'UPSTREAM_NETWORK', uncertain=writing) from error
        if response.status_code != 200:
            raise StarDayError('星动日服务 HTTP '+str(response.status_code), 'UPSTREAM_HTTP', uncertain=writing)
        if response.headers.get('Date'):
            try:
                self.clock = (parsedate_to_datetime(response.headers['Date']).astimezone(SHANGHAI), time.monotonic())
            except (ValueError, TypeError) as error:
                raise StarDayError('官方服务器时间格式异常。', 'SERVER_TIME_INVALID', uncertain=writing) from error
        try:
            value = response.json()
        except ValueError as error:
            raise StarDayError('官方响应格式异常，请在 APP 核实结果。', 'UPSTREAM_FORMAT', uncertain=writing) from error
        if not isinstance(value, dict) or 'status' not in value:
            raise StarDayError('官方响应格式异常，请在 APP 核实结果。', 'UPSTREAM_FORMAT', uncertain=writing)
        if str(value['status']) == '-800':
            logging.getLogger(__name__).warning('Star Day upstream rate limit at %s', path)
        return value

    @staticmethod
    def _data(value, *, writing=False):
        code = str(value['status'])
        if code != '1':
            message = str(value.get('msg') or '官方暂未允许此操作')[:300]
            raise StarDayError(explain_rate_limit(message, code), code[:40], rejected=writing)
        return value.get('data')

    def _now(self):
        if self.clock is None:
            raise StarDayError('未取得官方服务器时间，本次未提交。', 'SERVER_TIME_MISSING')
        return self.clock[0]+timedelta(seconds=time.monotonic()-self.clock[1])

    def _inspect(self, client):
        profile = self._data(self._request(client, '/member/func/queryVipLevel.do', data={'sspId':'75628', 'tag':'qqtindex'}))
        if not isinstance(profile, dict) or 'userLevel' not in profile:
            raise StarDayError('未取得全球通等级，请稍后重试。', 'UPSTREAM_FORMAT')
        tier = str(profile['userLevel'])
        eligible = tier in TIERS
        tier_name = str(profile.get('levelName') or TIERS.get(tier) or ('非全球通用户' if tier in ('6', '7') else '未知等级'))
        result = {'date':self._now().strftime('%Y%m%d'), 'server_time':self._now().isoformat(),
            'tier':tier, 'tier_name':tier_name, 'eligible':eligible, 'today_state':'ineligible' if not eligible else 'unknown',
            'consecutive_days':None, 'month_claimed':None, 'benefit_state':'unavailable',
            'benefit_message':f'官方识别为{tier_name}（等级 {tier}），当前不符合星动日活动资格。', 'state':'ineligible' if not eligible else 'ready'}
        phone = str(profile.get('phone') or '')
        expected = self.credentials_provider()['phone']
        if re.fullmatch(r'1\d{10}', phone) and phone != expected:
            raise StarDayError('活动返回账号与共享账号不一致，本次已停止。', 'ACCOUNT_MISMATCH', 409)
        if not eligible:
            return {**result, 'message':result['benefit_message']}
        signin = self._data(self._request(client, '/sign/info'))
        if (not isinstance(signin, dict) or not isinstance(signin.get('signDates'), list) or not signin['signDates']
                or any(not isinstance(d, dict) or d.get('signStatus') not in (-1, 0, 1, 2) for d in signin['signDates'])):
            raise StarDayError('未取得有效的官方签到日历，本次未提交。', 'SIGN_STATE_MISSING')
        signed = not any(d['signStatus'] == 0 for d in signin['signDates'])
        rights = self._data(self._request(client, '/member/func/queryIndexReceive.do'))
        if not isinstance(rights, dict) or not isinstance(rights.get('prodList'), list) or 'enjoycollectabletimes' not in rights:
            raise StarDayError('未取得本月权益领取状态，本次未提交。', 'BENEFIT_STATE_MISSING')
        claimed = bool(rights['prodList']) or str(rights['enjoycollectabletimes']) == '0'
        first = rights['prodList'][0] if rights['prodList'] else {}
        name = str(first.get('qyName') or first.get('productName') or '') if isinstance(first, dict) else ''
        benefit_id = str(first.get('productId') or first.get('id') or '') if isinstance(first, dict) else str(first)
        # queryIndexReceive normally returns IDs, including hidden benefits.
        # Resolve their names for display; stock never determines claim status.
        if benefit_id and not name:
            try:
                for phase in range(1, 5):
                    product = next((p for p in self._products(client, (phase,), include_hidden=True)
                                    if p['id'] == benefit_id), None)
                    if product is not None:
                        name = product['name']
                        break
            except StarDayError:
                # A catalog failure must not erase a confirmed monthly claim.
                pass
        return {**result, 'date':self._now().strftime('%Y%m%d'), 'today_state':'signed' if signed else 'pending',
            'consecutive_days':sum(d['signStatus'] == 1 for d in signin['signDates']),
            'month_claimed':claimed, 'benefit_name':name, 'benefit_id':benefit_id,
            'benefit_state':'already_claimed' if claimed else 'available',
            'benefit_message':'本月权益机会已使用' if claimed else '本月尚有权益领取机会',
            'message':'今日已签到' if signed else '今日尚未签到'}

    def inspect(self):
        with self._session() as (client, _):
            return self._inspect(client)

    def _products(self, client, phases=range(1, 5), *, include_hidden=False):
        products = []
        for phase in phases:
            data = self._data(self._request(client, '/normal/normalNewProdList?phase='+str(phase)))
            if not isinstance(data, dict) or not isinstance(data.get('prodList'), list):
                raise StarDayError('官方权益列表格式异常。', 'UPSTREAM_FORMAT')
            for item in data['prodList']:
                if item.get('isHide') is True and not include_hidden:
                    continue
                ident, name = str(item.get('id') or ''), item.get('qyName')
                stock = item.get('stock')
                if (not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', ident) or not isinstance(name, str)
                        or stock is not None and (isinstance(stock, bool) or not isinstance(stock, (int, float)) or not math.isfinite(stock))):
                    raise StarDayError('官方权益字段格式异常。', 'UPSTREAM_FORMAT')
                levels = str(item.get('levelable') or '')
                products.append({'id':ident, 'name':name[:150], 'phase':phase, 'stock':stock,
                    'levels':levels, 'level_description':'、'.join(TIERS.get(l, l) for l in levels.split(',') if l) or '以官方资格为准',
                    'transferable':str(item.get('transferable')) == '1',
                    'is_random':item.get('isRandom') is True})
        return products

    def products(self):
        with self._session() as (client, _):
            return self._products(client)

    def _family(self, client):
        data = self._data(self._request(client, '/paidpurchase/familyCircle.action', data={'sspId':'75628'}))
        if not isinstance(data, dict):
            raise StarDayError('未取得有效的官方亲友列表，请稍后重试。', 'FAMILY_LIST_MISSING')
        family = data.get('familyList')
        if family is None:
            family = []
        if not isinstance(family, list):
            raise StarDayError('未取得有效的官方亲友列表，请稍后重试。', 'FAMILY_LIST_MISSING')
        members = []
        for item in family:
            if (not isinstance(item, dict) or not isinstance(item.get('familyMemNum'), str) or not item['familyMemNum']
                    or not isinstance(item.get('showFamilyMemNum'), str)):
                raise StarDayError('官方亲友字段格式异常，本次未提交。', 'FAMILY_LIST_INVALID')
            members.append({'id':hashlib.sha256(item['familyMemNum'].encode()).hexdigest(),
                            'phone_masked':item['showFamilyMemNum'], 'name':str(item.get('familyMemName') or '亲友')[:60],
                            'value':item['familyMemNum']})
        return members

    def gift_options(self):
        with self._session() as (client, _):
            observed = self._inspect(client)
            if not observed['eligible']:
                raise StarDayError(observed['message'], 'INELIGIBLE', 409)
            if observed['month_claimed']:
                raise StarDayError('该账号本月已经领取或赠送，不能再次使用本月机会。', 'MONTH_CLAIMED', 409)
            return {'can_send_other':observed['tier'] == '8', 'family':[
                {k:m[k] for k in ('id','phone_masked','name')} for m in self._family(client)]}

    def _gift_destination(self, client, snapshot, observed, recipient):
        if recipient['kind'] == 'family':
            member = next((m for m in self._family(client) if m['id'] == recipient['id']), None)
            if member is None:
                raise StarDayError('亲友列表已变化，请重新选择接收人。', 'FAMILY_CHANGED', 409)
            return member['value'], member['name']+' · '+member['phone_masked']
        if observed['tier'] != '8':
            raise StarDayError('官方仅允许卓越卡赠送给非亲友号码，请选择已添加的亲友。', 'FAMILY_ONLY', 409)
        phone = recipient['phone']
        if phone == snapshot['phone']:
            raise StarDayError('赠送号码不能是当前账号，请选择接收人。', 'INVALID_RECIPIENT', 400)
        return phone, phone[:3]+'****'+phone[-4:]

    def calibrate(self):
        """One read-only clock estimate for the whole round, never per account.

        HTTP Date has one-second resolution. Intersect successive timing bounds
        rather than treating a rounded Date as an exact millisecond clock.
        """
        with self._session() as (client, _):
            client.headers.update({'Cache-Control':'no-cache', 'Pragma':'no-cache'})
            lower, upper, count = float('-inf'), float('inf'), 0
            deadline = time.monotonic()+6
            for _ in range(12):
                sent = datetime.now(SHANGHAI).timestamp()
                self._data(self._request(client, '/normal/normalNewProdList?phase=1'))
                received = datetime.now(SHANGHAI).timestamp()
                if self.clock is None:
                    raise StarDayError('官方未返回校准时间，本轮未安排抢购。', 'SERVER_TIME_MISSING')
                server = self.clock[0].timestamp()
                lower, upper = max(lower, server-received), min(upper, server+1-sent)
                count += 1
                if lower > upper:
                    raise StarDayError('官方时间响应不一致，请重新校准。', 'CLOCK_UNSTABLE')
                if count >= 3 and upper-lower <= .15 or time.monotonic() >= deadline:
                    break
            return {'state':'ready', 'offset_ms':round((lower+upper)*500, 3),
                'uncertainty_ms':round((upper-lower)*500, 1), 'samples':count,
                'at':datetime.now(SHANGHAI).isoformat()}

    def signin(self, *, before_submit):
        with self._session() as (client, snapshot):
            observed = self._inspect(client)
            if not observed['eligible'] or observed['today_state'] == 'signed':
                return {**observed, 'state':'signed' if observed['today_state'] == 'signed' else 'skipped', 'uncertain':False}
            self._current(snapshot)
            if not before_submit('signin', observed['date']):
                return {**observed, 'state':'unknown', 'uncertain':True, 'message':'上次签到结果尚未确认，已查询官方记录，本次没有重复提交。'}
            self._data(self._request(client, '/sign/sign', json={'sspid':'75628'}, writing=True), writing=True)
            # A successful sign response is authoritative even if later reads fail.
            return {**observed, 'today_state':'signed', 'state':'signed', 'uncertain':False,
                    'consecutive_days':observed['consecutive_days']+1, 'message':'签到成功'}

    def claim(self, product_id, phase, *, before_submit, product_name='', recipient=None):
        """One immediate claim, checking monthly ownership without a stock gate."""
        with self._session() as (client, snapshot):
            observed = self._inspect(client)
            if observed['month_claimed']:
                return {**observed, 'state':'already_claimed', 'uncertain':False,
                        'message':'官方确认本月已领取权益，本次没有重复提交。', 'attempt_count':0}
            if not observed['eligible']:
                return {**observed, 'state':'skipped', 'uncertain':False, 'attempt_count':0}
            receive_phone, recipient_label, random = '', '', '0'
            if recipient is not None:
                product = next((p for p in self._products(client, (phase,), include_hidden=True) if p['id'] == product_id), None)
                if product is None or not product['transferable']:
                    raise StarDayError('官方当前不允许赠送此权益，本次未提交。', 'NOT_TRANSFERABLE', 409)
                random = '1' if product['is_random'] else '0'
                receive_phone, recipient_label = self._gift_destination(client, snapshot, observed, recipient)
            self._current(snapshot)
            if not before_submit('benefit', observed['date'][:6]):
                return {**observed, 'state':'unknown', 'uncertain':True, 'attempt_count':0,
                        'message':'上次权益领取结果尚未确认，已查询官方记录，本次没有重复提交。'}
            value = self._request(client, '/normal/initNormalNewQy', data={
                'productId':product_id, 'receivePhone':receive_phone, 'channelCode':'75628',
                'isRandom':random, 'sourece':'1' if recipient is not None else '2'}, writing=True)
            if str(value['status']) == '15':
                confirmed = self._inspect(client)
                claimed = confirmed['month_claimed']
                return {**confirmed, 'state':'already_claimed' if claimed else 'unknown',
                        'uncertain':not claimed, 'attempt_count':1,
                        'message':'官方确认本月已领取权益。' if claimed else '官方提示领取次数受限，结果待核实，本次不重复提交。'}
            self._data(value, writing=True)
            return {**observed, 'state':'success', 'month_claimed':True, 'benefit_name':product_name, 'benefit_id':product_id,
                    'benefit_recipient':recipient_label, 'benefit_mode':'gift' if recipient is not None else 'self',
                    'benefit_state':'already_claimed', 'uncertain':False, 'attempt_count':1,
                    'message':('赠送成功 · '+recipient_label+'，权益将在24小时内下发。') if recipient is not None else '权益领取成功，官方将在24小时内下发。'}

    def rush(self, product_id, phase, *, target_at, burst, before_submit, cancelled,
             start_monotonic=None, product_name='', recipient=None, is_random=False):
        with self._session() as (client, snapshot):
            # Match xbk.py: the product was resolved ONCE when configuring the
            # round. Account workers do not read inventory or eligibility.
            receive_phone, recipient_label = '', ''
            if recipient is not None:
                observed = self._inspect(client)
                if observed['month_claimed']:
                    return {**observed, 'state':'already_claimed', 'message':'本月已领取或赠送，本轮不重复提交。', 'uncertain':False}
                if not observed['eligible']:
                    return {**observed, 'state':'skipped', 'uncertain':False}
                receive_phone, recipient_label = self._gift_destination(client, snapshot, observed, recipient)
            target_at = target_at.astimezone(SHANGHAI)
            # Scheduler supplies the SAME monotonic deadline to every account.
            # No per-account clock estimate is used for dispatch.
            if start_monotonic is None:
                raise StarDayError('本轮尚未统一校准时间，本次未提交。', 'CLOCK_NOT_READY', 409)
            start = start_monotonic
            offsets = burst.get('offsets_ms', [i*burst['burst_interval']*1000 for i in range(burst['burst_count'])])
            end = start+max(offsets)/1000+20
            observed = {'date':target_at.strftime('%Y%m%d'), 'benefit_state':'available',
                        'benefit_message':'等待预约时间'}
            if start < time.monotonic()-10:
                return {**observed, 'state':'skipped', 'message':'已错过预约时间，本轮未提交。', 'uncertain':False}
            clients, queue = [], SimpleQueue()
            try:
                for _ in range(min(burst['burst_workers'], burst['burst_count'])):
                    worker = self.worker_session_factory()
                    clients.append(worker)
                    worker.headers.update(dict(client.headers))
                    worker.cookies.update(client.cookies)
                    queue.put(worker)
                owner = self
                class Prepared:
                    ready, result = True, observed
                    start_monotonic, end_monotonic = start, end
                    def local_time(self):
                        return (target_at+timedelta(seconds=time.monotonic()-start)).isoformat()
                    def send(self, *, before_submit):
                        worker = queue.get()
                        try:
                            owner._current(snapshot)
                            before_submit()
                            value = owner._request(worker, '/normal/initNormalNewQy', data={
                                'productId':product_id, 'receivePhone':receive_phone, 'channelCode':'75628',
                                'isRandom':'1' if is_random else '0', 'sourece':'1' if recipient is not None else '2'}, writing=True)
                            code, message = str(value['status']), str(value.get('msg') or '官方暂未允许领取')[:300]
                            if code == '1':
                                return {'state':'success', 'message':('赠送成功 · '+recipient_label+'，权益将在24小时内下发。') if recipient is not None else '权益领取成功，官方将在24小时内下发。', 'code':code}
                            if code == '-800':
                                return {'state':'terminal', 'message':explain_rate_limit(message, code), 'code':code}
                            if code == '15':
                                return {'state':'terminal', 'message':'本月已领取该权益，请核对官方领取记录。', 'code':'PRIZE_LIMIT_MONTH'}
                            # Only explicit, definite temporary refusals authorize
                            # the next planned attempt. Unknown replies stop.
                            retry = bool(re.search(r'尚未开始|未到.{0,4}时间|活动太火爆|服务繁忙|稍后重试', message))
                            terminal = bool(re.search(r'领完|库存不足|验证|登录|上限|资格|不符合', message))
                            return {'state':'retryable' if retry and not terminal else 'terminal', 'message':message, 'code':code}
                        finally:
                            queue.put(worker)
                    def reconcile(self):
                        result = owner._inspect(client)
                        if result.get('benefit_id') != product_id:
                            result['benefit_state'] = 'unknown'
                        return result
                # Commit the round's durable intent during preparation. The
                # timed workers must not queue behind SQLite/DPAPI at 10:00.
                reserved = before_submit('benefit', target_at.strftime('%Y%m'))
                def guard():
                    if not reserved:
                        raise StarDayError('本月已有未确认的领取提交，已停止重复请求。', 'RESULT_UNCERTAIN', 409, uncertain=True)
                # xbk.py is launched once per millisecond offset. These are
                # independent planned rounds, not retries gated by earlier replies.
                result = run_flash_burst(Prepared(), burst, guard, cancelled, result_key='benefit',
                                        followup_seconds=None, stop_on_result=False,
                                        planned_offsets=[offset/1000 for offset in offsets])
                if result['state'] == 'success':
                    result.update(month_claimed=True, benefit_name=product_name, benefit_id=product_id, benefit_recipient=recipient_label, benefit_mode='gift' if recipient is not None else 'self')
                return result
            finally:
                for worker in clients:
                    worker.close()
