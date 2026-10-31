"""APP SSO client for the official qwhdmark 31-day sign-in page.

Protocol source: qwhdmark/static/js/index.1f703412.js and
pages-qwhdmark-views-home-index.e83418db.js, observed 2026-10-29.
The legacy /do/mark activity is deliberately not used for the APP calendar.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from queue import SimpleQueue
import re
import time
from urllib.parse import urljoin, urlsplit

from http_transport import requests

from recharge.cmcc import APP_UA, SHANGHAI, parse_server_time


ENTRY = 'https://wx.10086.cn/qwhdhub/qwhdmark/1021122301'
BASE = 'https://wx.10086.cn/qwhdhub/api/mark/'
FLASH_PRODUCT = 'month-end-2yuan'
READ_PATHS = {'user/info', 'info/commonInfo', 'mark31/markstatus',
              'markSeckill/secConfig', 'markSeckill/todayMarkStatus'}
WRITE_PATHS = {'mark31/domark', 'markSeckill/redeem'}
RESULT_MESSAGES = {
    'PRIZE_NO_STOCK': '本场奖品已抢完',
    'NOT_TIME_IN': '当前不在官方活动时间内',
    'PRIZE_LIMIT_DAY': '当天领取次数已用完',
    'PRIZE_LIMIT_MONTH': '当月领取次数已用完',
    'PRIZE_RESTRIC_LIMIT': '官方领取次数已达上限',
    'WORK_ORDER_RESTRIC_LIMIT': '官方限制本次参与',
    'PRIZE_NO_CONFIG': '签到成功，今天暂无配置奖品',
    'PRIZE_ISSUE_FAILED': '奖品发放暂未完成，请在 APP 核实',
    'NET_ERROR': '官方服务繁忙，本次没有自动重发',
}


class CheckinError(Exception):
    def __init__(self, message, code='CHECKIN_ERROR', status=502, *, uncertain=False):
        super().__init__(message)
        self.code, self.status, self.uncertain = code, status, uncertain


def _official_url(value, base=ENTRY):
    target = urljoin(base, value)
    parts = urlsplit(target)
    if parts.scheme != 'https' or parts.netloc != 'wx.10086.cn' or parts.username or parts.password:
        raise CheckinError('活动认证返回了不支持的跳转地址', 'SSO_REDIRECT')
    return target


def _data(response):
    if response.get('code') != 'SUCCESS':
        code = str(response.get('status') or response.get('code') or 'UPSTREAM_FAILED')
        raise CheckinError(RESULT_MESSAGES.get(code, '官方暂时未允许此操作（'+code[:60]+'）'), code)
    return response.get('data')


def _coupon(zone):
    prize = zone.get('prize') or {}
    # This is the free coupon shown in the user's page, not a configurable
    # purchase endpoint. Future unrelated products are never auto-selected.
    return (re.sub(r'\s+', '', str(prize.get('name', ''))) == '2元话费券'
            and prize.get('id') is not None
            and not any(float(prize.get(key) or 0) > 0 for key in ('price', 'payAmount', 'pointsCost')))


def _zone_time(value):
    return parse_server_time(value)


def _in_window(zone, now):
    start, end = _zone_time(zone.get('startTime')), _zone_time(zone.get('endTime'))
    return bool(start is not None and end is not None and start <= now < end)


class CheckinClient:
    def __init__(self, credentials_provider, *, session_factory=None, worker_session_factory=None, credential_cache=None):
        self.credentials_provider = credentials_provider
        self.credential_cache = credential_cache
        self.session_factory = session_factory or (lambda: requests.Session(impersonate='chrome'))
        # A leased session is used by one worker at a time, so its warm curl
        # connection can move between pool threads without sharing a handle.
        self.worker_session_factory = worker_session_factory or (lambda: requests.Session(impersonate='chrome', use_thread_local_curl=False))

    def _snapshot(self):
        value = self.credentials_provider()
        if (not isinstance(value, dict) or not isinstance(value.get('cookie'), str)
                or not re.search(r'(?i)(?:^|;)\s*JSESSIONID=', value['cookie'])
                or not re.fullmatch(r'1[3-9]\d{9}', str(value.get('phone', '')))
                or not isinstance(value.get('profile', {}), dict)):
            raise CheckinError('共享 APP 登录不可用，请在账号管理重新登录', 'APP_LOGIN_REQUIRED', 401)
        return {**value, 'profile': dict(value.get('profile', {}))}

    def _current(self, snapshot):
        latest = self.credentials_provider()
        if (latest is None or latest.get('phone') != snapshot['phone']
                or self.credential_cache is None and latest.get('cookie') != snapshot['cookie']):
            raise CheckinError('共享账号凭据已更新，请重试以使用最新登录态', 'APP_CREDENTIALS_CHANGED', 409)

    @staticmethod
    def _follow(client, url):
        for _ in range(10):
            url = _official_url(url)
            response = client.get(url, timeout=(10, 20), allow_redirects=False)
            if response.status_code in {301, 302, 303, 307, 308}:
                url = _official_url(response.headers.get('location', ''), url)
                continue
            if response.status_code != 200:
                raise CheckinError('签到活动入口 HTTP '+str(response.status_code), 'UPSTREAM_HTTP')
            return response
        raise CheckinError('活动认证跳转次数过多', 'SSO_REDIRECT')

    def _post(self, client, path, body=None, *, snapshot=None, before_submit=None):
        if path not in READ_PATHS | WRITE_PATHS:
            raise ValueError('Unsupported check-in endpoint')
        writing = path in WRITE_PATHS
        if writing:
            self._current(snapshot)
            if before_submit is not None:
                before_submit()
        try:
            response = client.post(BASE+path, json=body or {},
                                   headers={'Content-Type': 'application/json;charset=UTF-8',
                                            'x-requested-with': 'XMLHttpRequest', 'login-check': '1'},
                                   timeout=(10, 20), allow_redirects=False)
        except requests.RequestsError as error:
            raise CheckinError('请求中断，结果待核实；本次没有自动重发' if writing else '连接签到服务失败，请稍后重试',
                               'RESULT_UNCERTAIN' if writing else 'UPSTREAM_NETWORK', uncertain=writing) from error
        if response.status_code not in (200,401):
            raise CheckinError('签到服务 HTTP '+str(response.status_code), 'UPSTREAM_HTTP', uncertain=writing)
        try:
            value = {'code':'GOTO_LOGIN'} if response.status_code == 401 else response.json()
        except ValueError as error:
            raise CheckinError('官方响应格式异常，请在 APP 核实', 'UPSTREAM_FORMAT', uncertain=writing) from error
        if not isinstance(value, dict):
            raise CheckinError('官方响应格式异常，请在 APP 核实', 'UPSTREAM_FORMAT', uncertain=writing)
        code = str(value.get('status') or value.get('code') or '')
        if value.get('code') == 'GOTO_LOGIN':
            if self.credential_cache is not None and getattr(client,'_credential_binding',None):
                if not writing and self.credential_cache.recover(client):
                    return self._post(client,path,body,snapshot=snapshot)
                self.credential_cache.invalidate(client)
            raise CheckinError('签到活动会话尚未恢复，请校验并重连账号', 'ACTIVITY_LOGIN_REQUIRED', 401)
        if re.search('CAPTCHA|VERIFY|RISK|CHECK_CODE', code, re.I):
            raise CheckinError('官方要求验证，请在 APP 完成验证后重试', 'VERIFICATION_REQUIRED', 409, uncertain=writing)
        return value

    def _authenticate(self, client, snapshot):
        page = self._follow(client, ENTRY)
        match = re.search(r"var\s+loginPath\s*=\s*['\"]([^'\"]+)['\"]", page.text)
        if not match or not match[1].startswith('/appTokenLogin?sid='):
            raise CheckinError('签到活动未返回 APP 登录入口', 'APP_SSO_ENTRY')
        self._current(snapshot)
        profile = snapshot['profile']
        response = client.post(_official_url('/qwhdsso'+match[1]), json={
            'jwtToken': None, 'token': snapshot['cookie'],
            'provinceCode': str(profile.get('provinceCode', '')),
            'cityCode': str(profile.get('cityCode', '')),
            'userCheckId': format(int(snapshot['phone']), 'x'),
            'carrierOperator': str(profile.get('carrierOperator') or '002'),
            'appVersionCode': '12.5.6', 'took': 0,
        }, timeout=(10, 25), allow_redirects=False)
        if response.status_code != 200:
            raise CheckinError('APP 活动认证 HTTP '+str(response.status_code), 'APP_SSO_HTTP')
        payload = _data(response.json())
        if not isinstance(payload, dict) or not isinstance(payload.get('url'), str):
            raise CheckinError('APP 活动认证响应格式无效', 'APP_SSO_FORMAT')
        self._follow(client, payload['url'])
        identity = _data(self._post(client, 'user/info', {'appVersion': '12.5.6', 'miniVersion': ''}))
        if not isinstance(identity, dict):
            raise CheckinError('签到活动身份响应格式无效', 'UPSTREAM_FORMAT')
        # The current H5 keeps `mobile` opaque and renders nickName as the
        # masked phone. Retain a phone check alongside the fresh SSO token
        # and userCheckId binding instead of comparing that opaque value.
        mobile = str(identity.get('mobile') or '')
        masked = snapshot['phone'][:3]+'****'+snapshot['phone'][-4:]
        same_phone = (mobile == snapshot['phone'] if re.fullmatch(r'1[3-9]\d{9}', mobile)
                      else bool(mobile) and identity.get('nickName') == masked)
        if not same_phone or identity.get('channel') != 'app':
            raise CheckinError('签到活动返回的账号或渠道不一致', 'ACCOUNT_MISMATCH', 409)
        self._current(snapshot)
        return identity

    @contextmanager
    def _session(self):
        snapshot = self._snapshot()
        client = self.session_factory()
        client.headers.update({'User-Agent':APP_UA,'Origin':'https://wx.10086.cn','Referer':ENTRY})
        try:
            if self.credential_cache is None:
                self._authenticate(client,snapshot)
            else:
                self.credential_cache.connect('qwhdmark:1021122301',snapshot,client,
                    lambda:self._authenticate(client,snapshot))
            yield client,snapshot
        except requests.RequestsError as error:
            raise CheckinError('连接 APP 活动认证服务失败，请稍后重试','UPSTREAM_NETWORK') from error
        except (ValueError,TypeError,KeyError) as error:
            raise CheckinError('签到活动响应格式无效，请稍后重试','UPSTREAM_FORMAT') from error
        finally:
            client.close()

    def _inspect(self, client):
        common = _data(self._post(client, 'info/commonInfo'))
        if not isinstance(common, dict):
            raise CheckinError('未取得官方签到配置', 'UPSTREAM_FORMAT')
        now = parse_server_time(common.get('sysTime'))
        if now is None:
            raise CheckinError('未取得官方服务器时间，本次未提交', 'SERVER_TIME_MISSING')
        if common.get('isMark31') not in (True, 1, '1'):
            raise CheckinError('当前账号未开放本页的每日签到活动', 'ACTIVITY_UNAVAILABLE', 409)
        month = _data(self._post(client, 'mark31/markstatus'))
        if (not isinstance(month, dict) or not isinstance(month.get('markstatus'), list)
                or not isinstance(month.get('userinfo'), dict)):
            raise CheckinError('未取得本月签到记录', 'UPSTREAM_FORMAT')
        today = now.strftime('%Y%m%d')
        day = next((item for item in month['markstatus'] if item.get('date') == today), None)
        if day is None or str(day.get('status')) not in {'0', '1'}:
            raise CheckinError('未取得今天的签到状态，本次未提交', 'TODAY_STATE_MISSING')
        # The official current-day test accepts only status 1. Status 2 is a
        # historical re-sign marker, not proof of today's ordinary sign-in.
        signed = str(day['status']) == '1'
        count = month['userinfo'].get('accumulateTimes')
        count = int(count) if count is not None else None
        config = _data(self._post(client, 'markSeckill/secConfig'))
        zones = (config or {}).get('secKillData', {}).get('secKillZones', []) if isinstance(config, dict) else []
        eligible = [zone for zone in zones if _coupon(zone)]
        open_zones = [zone for zone in eligible if _in_window(zone, now)]
        claimed = any(str(zone['prize'].get('status')) == '1' for zone in eligible)
        stock = any(float(zone['prize'].get('leaveStock') or 0) > 0 for zone in open_zones)
        flash_state = 'already_claimed' if claimed else 'available' if stock and signed else 'unavailable'
        flash_message = ('本月已领取' if claimed else '当前可参与' if stock and signed
                         else '先完成今日签到' if open_zones and not signed else '当前没有开放的秒杀场次')
        products = [{'id': FLASH_PRODUCT, 'name': '2元话费券', 'cost': '免费',
                     'label': '免费领券 · 当日先签到 · 以官方活动时间和领取上限为准',
                     'selectable': True, 'available': bool(stock and signed and not claimed)}] if eligible else []
        return {'date': today, 'server_time': now.isoformat(),
                'today_state': 'signed' if signed else 'unsigned', 'month_count': count,
                'signin_message': '今日已签到' if signed else '今日尚未签到',
                'flash_state': flash_state, 'flash_message': flash_message,
                'products': products}, eligible

    def inspect(self):
        with self._session() as (client, snapshot):
            result, _ = self._inspect(client)
            self._current(snapshot)
            return result

    def signin(self, *, before_submit=None):
        with self._session() as (client, snapshot):
            result, _ = self._inspect(client)
            if result['today_state'] == 'signed':
                return {**result, 'state': 'already_signed', 'message': '今日已签到，已跳过重复提交', 'submitted': False}
            response = self._post(client, 'mark31/domark', {'date': result['date']},
                                  snapshot=snapshot, before_submit=before_submit)
            updated, _ = self._inspect(client)
            if updated['today_state'] == 'signed':
                prize = (response.get('data') or {}).get('markPrize') or {}
                name = prize.get('name') or prize.get('prizeName') or ''
                message = '签到成功'+('，获得'+str(name) if name else '')
                if response.get('status') in {'PRIZE_NO_CONFIG', 'PRIZE_NO_STOCK'}:
                    message = '签到成功，今日奖品未发放或已抢完'
                return {**updated, 'state': 'signed', 'message': message, 'submitted': True}
            code = str(response.get('status') or response.get('code') or 'UNKNOWN')
            return {**updated, 'state': 'failed', 'message': RESULT_MESSAGES.get(code, '官方尚未确认签到成功（'+code[:60]+'）'), 'submitted': True}

    def flash(self, product_id, *, before_submit=None, burst=None, cancelled=None, target_at=None):
        if product_id != FLASH_PRODUCT:
            raise CheckinError('请选择已确认的月末2元话费券', 'INVALID_PRODUCT', 400)
        if burst is not None:
            from checkin_flash import run_flash_burst
            with self._session() as (client, snapshot):
                prepared = PreparedFlash(self, client, snapshot)
                try:
                    prepared.prepare(burst['burst_workers'], target_at)
                    return run_flash_burst(prepared, burst, before_submit, cancelled or (lambda: False))
                finally:
                    prepared.close()
        with self._session() as (client, snapshot):
            result, zones = self._inspect(client)
            if result['today_state'] != 'signed' or result['flash_state'] != 'available':
                return {**result, 'state': 'skipped', 'message': result['flash_message'], 'submitted': False}
            now = datetime.fromisoformat(result['server_time'])
            zone = next(zone for zone in zones if _in_window(zone, now)
                        and str(zone['prize'].get('status')) != '1'
                        and float(zone['prize'].get('leaveStock') or 0) > 0)
            response = self._post(client, 'markSeckill/redeem',
                                  {'secId': str(zone['id']), 'prizeId': zone['prize']['id']},
                                  snapshot=snapshot, before_submit=before_submit)
            if response.get('code') == 'SUCCESS':
                return {**result, 'state': 'success', 'flash_state': 'already_claimed',
                        'flash_message': '秒杀领取成功，请在 APP 我的奖品中查看',
                        'message': '秒杀领取成功，请在 APP 我的奖品中查看', 'submitted': True}
            code = str(response.get('status') or response.get('code') or 'UNKNOWN')
            return {**result, 'state': 'skipped', 'message': RESULT_MESSAGES.get(code, '官方未允许领取（'+code[:60]+'）'), 'submitted': True}


class PreparedFlash:
    def __init__(self, owner, client, snapshot):
        self.owner, self.client, self.snapshot = owner, client, snapshot
        self.ready, self.sessions, self.available = False, [], SimpleQueue()

    def prepare(self, workers, target_at):
        self.result, zones = self.owner._inspect(self.client)
        now = datetime.fromisoformat(self.result['server_time'])
        if self.result['today_state'] != 'signed':
            self.result['flash_message'] = '先完成今日签到，本轮未发送'
            return
        if self.result['flash_state'] == 'already_claimed':
            return
        target = target_at or now.replace(hour=12, minute=0, second=0, microsecond=0)
        eligible = [zone for zone in zones if _in_window(zone, target)
                    and _zone_time(zone['endTime']) > now
                    and str(zone['prize'].get('status')) != '1'
                    and float(zone['prize'].get('leaveStock') or 0) > 0]
        if not eligible:
            self.result['flash_message'] = '本场未开放或奖品已抢完，本轮未发送'
            return
        self.zone = min(eligible, key=lambda zone: _zone_time(zone['startTime']))
        best = None
        for _ in range(workers):
            worker = self.owner.worker_session_factory()
            self.sessions.append(worker)
            worker.headers.update(self.client.headers)
            worker.cookies.update(self.client.cookies)
            began = time.monotonic()
            common = _data(self.owner._post(worker, 'info/commonInfo'))
            received = time.monotonic()
            server = parse_server_time(common.get('sysTime')) if isinstance(common, dict) else None
            if server is None:
                raise CheckinError('预热未取得官方时间，本轮未提交', 'SERVER_TIME_MISSING')
            sample = (received-began, received, server)
            if best is None or sample[0] < best[0]:
                best = sample
            self.available.put(worker)
        self.owner._current(self.snapshot)
        _, anchor, server = best
        # Anchor at receipt, without subtracting an assumed network delay. This
        # follows the official clock and never deliberately sends before open.
        self.start_monotonic = anchor + (max(target, _zone_time(self.zone['startTime']))-server).total_seconds()
        self.end_monotonic = anchor + (_zone_time(self.zone['endTime'])-server).total_seconds()
        self.ready = True

    @staticmethod
    def local_time():
        return datetime.now(SHANGHAI).isoformat(timespec='milliseconds')

    def send(self, *, before_submit):
        worker = self.available.get()
        try:
            response = self.owner._post(worker, 'markSeckill/redeem',
                {'secId': str(self.zone['id']), 'prizeId': self.zone['prize']['id']},
                snapshot=self.snapshot, before_submit=before_submit)
        finally:
            self.available.put(worker)
        if response.get('code') == 'SUCCESS':
            return {'state': 'success', 'code': 'SUCCESS', 'message': '秒杀领取成功，请在 APP 我的奖品中查看'}
        code = str(response.get('status') or response.get('code') or 'UNKNOWN')
        message = '官方服务繁忙' if code == 'NET_ERROR' else RESULT_MESSAGES.get(code, '官方未允许领取（'+code[:60]+'）')
        return {'state': 'retryable' if code in ('NET_ERROR', 'NOT_TIME_IN') else 'terminal', 'code': code, 'message': message}

    def reconcile(self):
        result, _ = self.owner._inspect(self.client)
        return result

    def close(self):
        for worker in self.sessions:
            worker.close()
