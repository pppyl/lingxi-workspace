"""Official sign-in task rewards and phone credit redemption.

Protocol source: qwhdmark index.3bd4c264.js, task-index.2ffa009f.js,
and exchangePrizes-index.13292536.js. Read contract observed 2026-11-24.
"""

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
from threading import RLock
import time
from urllib.parse import urljoin, urlsplit

from recharge.cmcc import parse_server_time
from workspace.accounts.storage import LoginStorageError
from workspace.feature_runtime import ThreadPoolExecutor

from .client import CheckinError, _data
from .activity_tasks import OPEN_TASKS, complete_activity_browse
from .cloud import CLOUD_TASKS, supported_task as cloud_task_supported
from .migu import TASK_NAME as MIGU_TASK_NAME, TASK as MIGU_TASK
from .baidu_map import TASK_NAME as BAIDU_MAP_TASK_NAME, TASK as BAIDU_MAP_TASK
from .taobao import TASK_NAME as TAOBAO_TASK_NAME, TASK as TAOBAO_TASK
from .cainiao import TASK_NAME as CAINIAO_TASK_NAME, TASK as CAINIAO_TASK
from .kuaishou import TASK_NAME as KUAISHOU_TASK_NAME, TASK as KUAISHOU_TASK
from .video import VIDEO_TASKS


TASK_LIST = 'task/taskList'
CATALOG = 'task/getExchangeList'
AWARD = 'task/getTaskAward'
EXCHANGE = 'task/exchange'
STOCK = 'task/checkStock'
TASK_INFO = 'task/taskInfo'
FINISH = 'task/finishTask'
QUERY_TASKS = {
    '查询我的套餐': ('usage', 'wx.10086.cn', '/cmcc-service/app-pages/main/myPackage'),
    '查查我的余量': ('usage', 'wx.10086.cn', '/cmcc-service/app-pages/main/marginQuery'),
    '浏览话费余额': ('balance', 'h.app.coc.10086.cn', '/cmcc-app/app-pages/balanceQuery.html'),
    '查询本月账单': ('bill', 'h.app.coc.10086.cn', '/cmcc-app/personalBill/phoneBillsNew.html'),
    '一键通查': ('overview', 'wx.10086.cn', '/cmcc-service/app-pages/extra/queryPolymerize'),
}
QUERY_REDIRECTS = {
    '浏览话费余额': ('wx.10086.cn', '/cmcc-service/app-pages/main/balanceQuery'),
    '查询本月账单': ('wx.10086.cn', '/cmcc-service/app-pages/main/phoneBills'),
}
PAGE_TASKS = {
    '装宽带送12个月月卡': ('2', '3', 'wx.10086.cn', '/website/businessPlatform/home/broadband/broadbandBooking'),
    '抢至高9元充值券': ('8', '1', 'wx.10086.cn', '/qwhdhub/diy-client/1126037461'),
    '周周打卡领话费': ('8', '1', 'wx.10086.cn', '/qwhdhub/diy-client/1126095600'),
    '参加周周领权益': ('8', '3', 'dev.coc.10086.cn', '/coc3/canvas/rightsmarket-h5-canvas/online/zzcj'),
    '多重网龄礼月月领': ('8', '2', 'dev.coc.10086.cn', '/coc/web6/ageFeedbackV2/'),
    '追剧至高抽百元话费': ('8', '1', 'wx.10086.cn', '/qwhdhub/diy-client/1126082530'),
    '移动爱购精选爆品': ('8', '1', 'dev.coc.10086.cn', '/coc3/canvas/rightsmarket-h5-canvas/online/bpzq'),
    '去游戏中心玩游戏': ('8', '2', 'wx.10086.cn', '/qwhdhub/diy-client/1126036587'),
    '贵州福利中心': ('8', '1', 'plus.migu.cn', '/c/0g2iwl'),
    '去浏览好礼月月领': ('8', '2', 'dev.coc.10086.cn', '/coc3/canvas/rightsmarket-h5-canvas/online/26hlyyltybld'),
    '去浏览幸运三日签': ('8', '2', 'dev.coc.10086.cn', '/coc3/canvas/rightsmarket-h5-canvas/online/26xysrqtybld'),
}
PAGE_REDIRECTS = {
    '去游戏中心玩游戏': ('wx.10086.cn', '/qwhdhub/diy-client/1126086939'),
    '贵州福利中心': ('m.miguvideo.com', '/mgs/promotion/provincevml/prd/index.html'),
}

# Protocol findings as of 2026-11-25, not account-specific execution results
# or runnable adapters. Evidence: docs/protocols/CHECKIN_BEANS_PROTOCOL.md.
TASK_LIMITATIONS = {
    '头条极速版刷视频': '入口调起头条极速版；实际任务查询、观看进度及合作回传尚未复刻。',
    '去支付宝施肥': '已定位支付宝场景授权流程，仍需取得该任务的有效业务入口和真实施肥完成回执。',
    '去芭芭农场领水果': '本人淘宝资料查询有效，但按官方页面配置查询农场仍返回 FAIL_SYS_UNAUTHORIZED_ENTRANCE；需要农场专属入口授权及真实操作回执。',
    '头条看热门视频赚钱': '入口调起头条；实际任务查询、观看进度及合作回传尚未复刻。',
    '打开快影即可赚金币': '入口为快影原生 KRN 福利中心；独立任务模块、认证及完成回传尚未复刻。',
    '去大众点评吃喝玩乐': '入口进入点评原生任务模块；网页仅负责调起，实际任务接口和完成回执尚未接通。',
    '去美团视频领现金': '入口调起美团视频；实际视频任务、有效播放进度和回传尚未接通。',
    '去闲鱼签到兑好物': '已定位闲鱼金币页；拉端 SDK 的 external.action.record 只记录调起，不能代替真实签到；金币签到协议与完成回执仍待接通。',
    '淘宝闪购果园领水果': '已定位淘宝闪购果园；可核对本人淘宝身份，闪购任务承接和真实果园操作回执仍待接通。',
    '去好车主签到领积分': '入口进入好车主小程序；其统一登录和微信身份不能用移动活动令牌代替，尚未接通。',
    '去京东金融领福利': '已找到京东金融云猪业务页；京东身份同步、实际业务任务及合作回执仍待接入。',
    '去京东瓜分千万京豆': '已找到京东种豆得豆业务页；京东身份同步、实际任务条件与操作回执仍待接入。',
    '去星图金融领现金': '当前核对到星图金融拉端页；实际业务任务、认证和回传接口尚未定位完整。',
    '去蚂蚁森林收能量': '入口进入支付宝小程序；尚无本人的支付宝授权和收能量结果，当前未接通。',
    '去爱奇艺极速版赚钱': '业务页读取爱奇艺登录信息；尚无可用爱奇艺身份，任务进度和回传未接通。',
    '去UC极速版领现金': '已定位新版福利页及 /task/noticeByToken；UC 的 kps_wg 身份、真实任务查询和回传仍待接通。',
    '去淘宝特价版0元购': '本人授权已通过鸡场任务列表查询；移动网页中转返回广告计划不存在，任务 67 的原生承接与完成回执仍未接通，未按标题推断需要下单。',
    '去一淘签到领现金': '当前为一淘拉端入口；本人的一淘签到状态、签到提交和合作回执尚未接通。',
    '去点淘赚零花钱': '当前为点淘拉端入口；实际业务任务、有效进度和回传尚未接通。',
    '去河马剧场看剧赚钱': '入口调起河马剧场；实际剧集任务、播放进度和合作回传尚未复刻。',
    '去西瓜视频边看边赚': '入口调起西瓜视频；实际任务查询、播放进度和合作回传尚未复刻。',
    '去逛短视频领现金': '已解析到淘宝逛逛原生页面；本人淘宝授权可复用，但实际视频任务、有效播放进度及完成回传仍待接通。',
    '去QQ阅读领福利': '独立 QQ 阅读登录尚未接通；协议核对时福利查询返回登录鉴权失败（-2）。',
    '看搞笑视频赚钱': '入口调起合作 App；其真实观看任务和有效完成回执尚未复刻。',
    '登王者云游戏领红包': '入口经云游戏配置调起小程序；尚无有效游戏会话，实际任务和完成回执未接通。',
    '去夸克天天领现金': '福利页缺少夸克登录参数时不查询任务；尚无合法夸克身份及原生签名，当前未接通。',
    '来趣头条赚金币': '官方配置将该入口转入支付宝小程序任务中心；配置读取成功不代表任务完成，授权和回执未接通。',
    '去百度浏览资讯': '已定位百度任务详情接口；客户端身份及风控参数尚未接通，不能按标题推断实际任务条件。',
    '去百度极速版领红包': '已定位百度极速版任务详情接口；客户端身份及风控参数尚未接通，尚无有效完成回执。',
    '去番茄音乐免费听歌': '入口调起番茄音乐；实际听歌任务、播放进度和合作回传尚未复刻。',
    '去UC浏览器领现金': '旧版查询返回 404 后已找到新版 /task/noticeByToken；UC 原生 kps_wg 身份、当前业务查询和有效完成回执仍待接通。',
    '去番茄畅听听书赚钱': '入口调起番茄畅听；实际听书任务、播放进度和合作回传尚未复刻。',
    '登携程看笔记领奖励': '入口调起携程原生任务模块；网页仅负责调起，账号身份、笔记任务及回传尚未接通。',
}


def _task_support(item):
    if item['taskName'] == KUAISHOU_TASK_NAME:
        if (str(item['taskCategory']), str(item['taskType']), item['jumpUrl']) == KUAISHOU_TASK:
            return True, '自动查询并承接本人快手回调任务，核对移动官方进度后领豆；运行无需手机。'
        return False, '官方快手任务入口已变化，需要重新核对。'
    if item['taskName'] == CAINIAO_TASK_NAME:
        if (str(item['taskCategory']), str(item['taskType']), item['jumpUrl']) == CAINIAO_TASK:
            return True, '复用本人兼容授权，核对菜鸟关联身份并访问任务首页，提交官方移动渠道回调后核对领豆；不会用普通签到替代。'
        return False, '官方菜鸟任务入口已变化，需要重新核对。'
    if item['taskName'] == TAOBAO_TASK_NAME:
        if (str(item['taskCategory']), str(item['taskType']), item['jumpUrl']) == TAOBAO_TASK:
            return True, '复用本账号一次性授权的淘宝登录，查询本人资料、访问真实活动并调用官方合作回调，核对领豆；运行无需手机。'
        return False, '官方淘宝任务入口已变化，需要重新核对。'
    if item['taskName'] == BAIDU_MAP_TASK_NAME:
        if (str(item['taskCategory']), str(item['taskType']), item['jumpUrl']) == BAIDU_MAP_TASK:
            return True, '使用一次性同步并按账号保存的本人百度地图登录，查询真实金币任务页，按官方签名回传后核对领豆；运行无需手机。'
        return False, '官方百度地图任务入口已变化，需要重新核对。'
    if item['taskName'] == MIGU_TASK_NAME:
        if (str(item['taskCategory']), str(item['taskType']), item['jumpUrl']) == MIGU_TASK:
            return True, '复用本账号咪咕登录，查询本人资料并访问活动页，使用官方接口回传后核对领豆结果。'
        return False, '官方咪咕任务入口已变化，需要重新核对。'
    if item['taskName'] in OPEN_TASKS:
        target = urlsplit(item.get('jumpUrl', ''))
        if (target.scheme == 'https' and
                (str(item['taskCategory']), str(item['taskType']), target.netloc, target.path) == OPEN_TASKS[item['taskName']]):
            return True, '自动浏览官方活动页，使用活动独立的任务令牌回传并核对领豆结果。'
        return False, '官方活动浏览入口已变化，需要重新核对。'
    if item['taskName'] in CLOUD_TASKS:
        if cloud_task_supported(item):
            return True, '自动以本账号登录云盘活动，实际访问活动页并满足浏览时长后核对进度和领豆。'
        return False, '官方云盘任务入口已变化，需要重新核对。'
    if item['taskName'] in VIDEO_TASKS:
        target = urlsplit(item.get('jumpUrl', ''))
        actual = (str(item['taskCategory']), str(item['taskType']), target.netloc, target.path)
        if target.scheme == 'https' and actual == VIDEO_TASKS[item['taskName']]:
            return True, '自动实际播放官方指定的视频或直播回放，满足时长后核对进度并领豆。'
        return False, '官方视频任务入口已变化，需要重新核对。'
    query = QUERY_TASKS.get(item['taskName'])
    if query:
        target = urlsplit(item.get('jumpUrl', ''))
        if (str(item['taskType']) == '3' and str(item['taskCategory']) == '2'
                and target.scheme == 'https' and (target.netloc, target.path) == query[1:]):
            return True, '自动查询真实业务数据，满足官方浏览时长后领豆。'
        return False, '官方任务入口或类型已变化，需要重新核对。'
    page = PAGE_TASKS.get(item['taskName'])
    if page:
        target = urlsplit(item.get('jumpUrl', ''))
        if (target.scheme == 'https'
                and (str(item['taskCategory']), str(item['taskType']), target.netloc, target.path) == page):
            return True, '自动访问指定活动页面，按官方任务参数确认进度并领豆。'
        return False, '官方活动入口已变化，需要重新核对。'
    if item['taskName'] in TASK_LIMITATIONS:
        return False, TASK_LIMITATIONS[item['taskName']]
    category = str(item['taskCategory'])
    reason = {
        '15': '需要微信小程序登录和订阅授权回执，当前 Python 接口尚未接通。',
        '4': '要求真实充值付款；当前按你的设置跳过付款任务。',
        '6': '需要对应合作 App 的登录、实际操作和任务回传，当前尚未接通。',
        '10': '需要视频、小程序或活动页的独立任务回传，当前尚未接通。',
        '11': '需要真实好友助力记录，当前没有符合要求的助力结果。',
        '8': '该活动页的任务完成流程尚未接入，需核对实际参与条件。',
        '2': '该页面的具体业务查询或办理流程尚未接入。',
    }.get(category, '该任务类型尚未接入，需要核对官方流程。')
    return False, reason

# The current official catalog and stock preflight reserve amounts up to 100.
STOCK_FLOOR = 100
REDEEMED = {'UNUSED', 'USED'}
REDEMPTION_REJECTIONS = {
    '1': '该账号不在官方兑换范围内。',
    '2': '该奖品今天已兑完。',
    '3': '该奖品今天已兑完。',
}


def _amount(value):
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise CheckinError('官方返回的 AI 豆数量无效，请刷新后再试。', 'UPSTREAM_FORMAT') from None
    if not amount.is_finite() or amount < 0:
        raise CheckinError('官方返回的 AI 豆数量无效，请刷新后再试。', 'UPSTREAM_FORMAT')
    return format(amount.normalize(), 'f')


def _tasks(client, session):
    payload = _data(client._post(session, TASK_LIST), stage='签到任务查询')
    tasks = []
    for item in payload['tasks']:
        award_type = str(item['awardType'])
        if award_type not in {'1', '2'}:
            continue  # Award type 3 is a make-up card.
        state = {'0': 'unfinished', '1': 'claimable', '2': 'claimed'}.get(str(item['status']), 'unknown')
        automatic, reason = _task_support(item)
        tasks.append({
            'id': str(item['taskId']), 'name': item['taskName'], 'group': item['taskClasifiName'],
            'reward': _amount(item['awardNum']), 'state': state,
            'progress': item.get('currentTimes'), 'target': item.get('totalTimes'),
            'automatic': automatic, 'reason': reason,
            'requirement': item.get('subtitle', ''),
        })
    return tasks


def _catalog(client, session):
    payload = _data(client._post(session, CATALOG, {'channel': 'app'}), stage='话费券查询')
    now = parse_server_time(payload['timeStamp'])
    if now is None:
        raise CheckinError('未取得兑换页的官方时间，请刷新后再试。', 'SERVER_TIME_MISSING')
    balance = _amount(payload['currentFee'])
    stocks = {str(item['id']): item['stock'] for item in payload['prizeStocks']}
    prizes = []
    for item in payload['prizes']:
        if str(item['prizeType']) != '1':
            continue
        prize_id = str(item['id'])
        status = item['status']
        cost = _amount(item['cost'])
        if status == 'WITHOUT':
            stock = stocks.get(prize_id)
            if now.hour < 8:
                state = 'before_open'
            elif stock is None or Decimal(cost) <= 0:
                state = 'unknown'
            elif int(stock) <= STOCK_FLOOR:
                state = 'sold_out'
            elif Decimal(balance) < Decimal(cost):
                state = 'insufficient'
            else:
                state = 'available'
        elif status == 'IN_QUEUE':
            state = 'processing'
        elif status in REDEEMED:
            state = 'redeemed'
        else:
            state = 'unknown'
        prizes.append({'id': prize_id, 'name': item['name'], 'cost': cost, 'state': state})
    return {
        'balance': balance, 'earned': _amount(payload['totalFee']), 'spent': _amount(payload['exchangeFee']),
        'server_time': now.isoformat(), 'prizes': prizes,
    }


class CheckinBeans:
    def __init__(self, scheduler, storage, query=None, video_factory=None, cloud_factory=None,
                 migu_factory=None, baidu_map_factory=None, taobao_factory=None, cainiao_factory=None,
                 kuaishou_factory=None):
        self.scheduler, self.storage = scheduler, storage
        self.query = query
        self.video_factory = video_factory
        self.cloud_factory = cloud_factory
        self.migu_factory = migu_factory
        self.baidu_map_factory = baidu_map_factory
        self.taobao_factory = taobao_factory
        self.cainiao_factory = cainiao_factory
        self.kuaishou_factory = kuaishou_factory
        self.pending = {}
        self.locks, self.lock = {}, RLock()
        self.data = (storage.load_activity_data('checkin-bean-tasks') if storage else None) or {}
        self.running = set()
        self.updating_logins = set()
        self.login_maintenance = None
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='checkin-beans')

    def _account_lock(self, login_id):
        with self.lock:
            return self.locks.setdefault(login_id, RLock())

    def _save(self, login_id, pending):
        if self.storage:
            self.storage.save_activity_data(f'checkin-ai-beans:{login_id}', pending)
        self.pending[login_id] = pending

    def begin_login_update(self,login_id):
        with self.lock:
            if login_id in self.running or login_id in self.updating_logins:return False
            pending=self.pending.get(login_id)
            if pending is None and self.storage:pending=self.storage.load_activity_data(f'checkin-ai-beans:{login_id}')
            if pending:return False
            account_lock=self._account_lock(login_id)
            if not account_lock.acquire(blocking=False):return False
            try:self.updating_logins.add(login_id)
            finally:account_lock.release()
            return True

    def end_login_update(self,login_id):
        with self.lock:self.updating_logins.discard(login_id)

    def _state(self, login_id, client, session):
        status = _catalog(client, session)
        status['tasks'] = _tasks(client, session)
        saved = self.data.get(login_id, {})
        kuaishou = saved.get('kuaishou', {})
        if (kuaishou.get('awaiting_mobile_confirmation') and kuaishou.get('date') == status['server_time'][:10]
                and any(row['name'] == KUAISHOU_TASK_NAME and row['state'] in {'claimable', 'claimed'}
                        for row in status['tasks'])):
            self._record(login_id, kuaishou={**kuaishou, 'awaiting_mobile_confirmation': False,
                                            'business_callback_verified': True})
        for row in status['tasks']:
            row['partner_service']={BAIDU_MAP_TASK_NAME:'baidu-map',TAOBAO_TASK_NAME:'taobao',CAINIAO_TASK_NAME:'taobao',
                                    KUAISHOU_TASK_NAME:'kuaishou'}.get(row['name'])
            deferred = (saved.get('deferred_tasks', {}).get(row['id'])
                        if saved.get('date') == status['server_time'][:10] else None)
            if row['state'] == 'unfinished' and deferred:
                row['reason'] = deferred['message']
                row['partner_login_required']=deferred['code'] in {'BAIDU_MAP_LOGIN_REQUIRED','TAOBAO_LOGIN_REQUIRED',
                                                                 'KUAISHOU_LOGIN_REQUIRED','KUAISHOU_QUERY_REJECTED'}
        status['tasks_available'] = bool(status['tasks'])
        if not status['tasks_available']:
            saved = self.data.get(login_id, {})
            if saved.get('date') == status['server_time'][:10]:
                status['tasks'] = deepcopy(saved.get('tasks', []))
                for row in status['tasks']:
                    if row['id'] in saved.get('awaiting_awards', {}):
                        row['state'] = 'claimable'
            status['task_warning'] = '官方当前未返回任务列表，正在显示最近核对记录。'
        if login_id not in self.pending:
            saved = self.storage.load_activity_data(f'checkin-ai-beans:{login_id}') if self.storage else None
            self.pending[login_id] = saved or {}
        pending = self.pending[login_id]
        if pending:
            if not any(row['id'] == pending['id'] for row in status['tasks']) or not status['tasks_available']:
                known = next((row for row in status['tasks'] if row['id'] == pending['id']), None)
                if known is not None and pending['kind'] in {'finish', 'claim'}:
                    info = _data(client._post(session, TASK_INFO, {'taskId': pending['id']}))
                    if str(info.get('taskId')) == pending['id']:
                        known['state'] = {'0': 'unfinished', '1': 'claimable', '2': 'claimed'}.get(str(info.get('status')), 'unknown')
            rows = status['prizes'] if pending['kind'] == 'redeem' else status['tasks']
            confirmed = {'claimable', 'claimed'} if pending['kind'] == 'finish' else (
                {'claimed'} if pending['kind'] == 'claim' else {'processing', 'redeemed'})
            # Sign-in beans expire at month end. A new monthly balance belongs
            # to a new period, even if the catalog reuses the same item ID.
            if (pending['month'] != status['server_time'][:7]
                    or pending.get('date') and pending['date'] != status['server_time'][:10]
                    or any(row['id'] == pending['id'] and row['state'] in confirmed for row in rows)):
                self._save(login_id, {})
        status['pending'] = bool(self.pending[login_id])
        return status

    def run(self, login_id, *, kind=None, item_id=None, cost=None):
        client = self.scheduler.client_factory(lambda: self.scheduler.credentials(login_id))
        with client._session() as (session, snapshot), self._account_lock(login_id):
            if kind is not None and login_id in self.updating_logins:
                raise CheckinError('当前账号正在更新合作方登录，请完成后再提交。','PARTNER_LOGIN_UPDATING',409)
            status = self._state(login_id, client, session)
            if kind is None:
                return status
            if kind == 'claim' and not status['tasks_available']:
                raise CheckinError('官方任务列表暂不可用，请稍后再领取。', 'UPSTREAM_TASKS_UNAVAILABLE', 503)
            if status['pending']:
                raise CheckinError('上次操作结果待核实，请先刷新官方状态。', 'RESULT_UNCERTAIN', 409, uncertain=True)

            is_claim = kind == 'claim'
            rows = status['tasks'] if is_claim else status['prizes']
            selected = next((row for row in rows if row['id'] == item_id), None)
            if not selected:
                raise CheckinError('官方本次列表未返回该项目，请刷新核对。', 'ITEM_UNAVAILABLE', 409)
            if is_claim:
                if selected['state'] != 'claimable':
                    raise CheckinError('官方尚未确认任务可领取，或本项已经领取。', 'TASK_NOT_CLAIMABLE', 409)
                path, body = AWARD, {'taskId': item_id}
            else:
                if selected['cost'] != cost or selected['state'] != 'available':
                    raise CheckinError('兑换条件已变化，请刷新后重新确认。', 'REDEEM_UNAVAILABLE', 409)
                body = {'prizeId': item_id}
                stock = _data(client._post(session, STOCK, body), stage='兑换库存核对')
                if not stock or int(stock['stock']) <= STOCK_FLOOR:
                    raise CheckinError('该奖品今天已兑完。', 'PRIZE_NO_STOCK', 409)
                path = EXCHANGE

            # Persist before the request so a lost response or process
            # restart cannot turn an unknown result into a repeat submission.
            pending = {'kind': kind, 'id': item_id, 'month': status['server_time'][:7]}
            if is_claim:
                pending['date'] = status['server_time'][:10]
            try:
                response = client._post(session, path, body, snapshot=snapshot,
                                        before_submit=lambda: self._save(login_id, pending))
            except CheckinError as error:
                if not error.uncertain and self.pending[login_id]:
                    self._save(login_id, {})
                raise
            if response.get('code') != 'SUCCESS':
                reason = REDEMPTION_REJECTIONS.get(str(response.get('data'))) if not is_claim else None
                if reason and response.get('code') == 'FAILED':
                    self._save(login_id, {})
                    raise CheckinError(reason, 'REDEEM_REJECTED', 409)
                code = str(response.get('status') or response.get('code') or 'RESULT_UNCERTAIN')
                raise CheckinError('官方未确认操作结果，请刷新查询。', code, 409, uncertain=True)
            try:
                status = self._state(login_id, client, session)
            except CheckinError as error:
                raise CheckinError('操作已提交，结果暂未核实。请刷新查询。', 'RESULT_UNCERTAIN', 409,
                                   uncertain=True) from error
            rows = status['tasks'] if is_claim else status['prizes']
            state = next((row['state'] for row in rows if row['id'] == item_id), None)
            if is_claim and state == 'claimed' and item_id in self.data.get(login_id, {}).get('awaiting_awards', {}):
                remaining = {key:value for key,value in self.data[login_id]['awaiting_awards'].items() if key != item_id}
                self._record(login_id, awaiting_awards=remaining)
            message = {
                'claimed': '已领取 AI 豆，官方任务状态已核对。',
                'redeemed': '已确认兑换，请到「我的卡券」查看。',
                'processing': '官方正在处理兑换，请稍后刷新核对。',
            }.get(state, '已提交，正在等待官方结果。请刷新查询。')
            return {**status, 'message': message}

    def task_status(self):
        with self.lock:
            return {'records': deepcopy(self.data), 'running': sorted(self.running), 'accounts': self.scheduler.accounts()}

    def _record(self, login_id, **values):
        with self.lock:
            updated = deepcopy(self.data)
            updated.setdefault(login_id, {}).update(values)
            if self.storage:
                self.storage.save_activity_data('checkin-bean-tasks', updated)
            self.data = updated

    def queue(self, login_id=None, *, automatic=False):
        now = self.scheduler.now()
        queued = 0
        for account in self.scheduler.accounts():
            key = account['id']
            if account['status'] != 'active' or login_id and key != login_id:
                continue
            with self.lock:
                old = self.data.get(key, {})
                if key in self.running or key in self.updating_logins:
                    continue
                if automatic and old.get('date') == now.date().isoformat():
                    retry = old.get('retry_at')
                    if old.get('state') != 'running' and (not retry or now < datetime.fromisoformat(retry)):
                        continue
                if self.scheduler.busy_login(key):
                    continue
                if not automatic:
                    self._record(key, attempts=0)
                self.running.add(key)
            self.executor.submit(self._perform_tasks, key)
            queued += 1
        return queued

    def tick(self, *, restored=()):
        if self.login_maintenance:self.login_maintenance()
        now = self.scheduler.now()
        clock = self.scheduler.data['automation']['signin_time']
        hour, minute = map(int, clock.split(':'))
        if now >= now.replace(hour=hour, minute=minute, second=0, microsecond=0):
            for login_id in restored:
                with self.lock:
                    record = self.data.get(login_id, {})
                    auth_codes = {
                        'APP_LOGIN_REQUIRED', 'ACTIVITY_LOGIN_REQUIRED', 'BILLS_LOGIN_REQUIRED',
                        'USAGE_LOGIN_REQUIRED', 'VIDEO_LOGIN_REQUIRED', 'CLOUD_LOGIN_REQUIRED', 'MIGU_LOGIN_REQUIRED',
                        'BAIDU_MAP_LOGIN_REQUIRED','TAOBAO_LOGIN_REQUIRED','KUAISHOU_LOGIN_REQUIRED'}
                    auth_failed = bool(auth_codes & ({record.get('code')} | {
                        item.get('code') for item in record.get('deferred_tasks', {}).values()}))
                if auth_failed:
                    self.queue(login_id)
            self.queue(automatic=True)

    def _browse(self, login_id, client, session, snapshot, item, month):
        task_id = str(item['taskId'])
        info = _data(client._post(session, TASK_INFO, {'taskId': task_id}), stage='查询任务参数')
        expected_type = str(item['taskCategory'])
        provinces = {'0', '1'} if expected_type == '8' or item['taskName'] in CLOUD_TASKS else {'0'}
        if (str(info['taskId']) != task_id or str(info['taskType']) != expected_type
                or str(info['provinceTask']) not in provinces):
            raise CheckinError('官方任务流程已变化，未提交完成记录。', 'TASK_CHANGED', 409)
        duration = float(info['scanTime'])
        if not 0 <= duration <= 60:
            raise CheckinError('官方浏览时长超出已支持范围。', 'TASK_CHANGED', 409)
        start = time.monotonic()
        migu_task = item['taskName'] == MIGU_TASK_NAME
        baidu_map_task = item['taskName'] == BAIDU_MAP_TASK_NAME
        taobao_task = item['taskName'] == TAOBAO_TASK_NAME
        cainiao_task = item['taskName'] == CAINIAO_TASK_NAME
        kuaishou_task = item['taskName'] == KUAISHOU_TASK_NAME
        partner_task = migu_task or baidu_map_task or taobao_task or cainiao_task or kuaishou_task
        if not partner_task:
            target = item['jumpUrl']
            for _ in range(3):
                page = session.get(target, timeout=(10, 20), allow_redirects=False)
                if page.status_code not in {301, 302, 303, 307, 308}:
                    break
                target = urljoin(target, page.headers.get('location', ''))
                parts = urlsplit(target)
                redirects = {**QUERY_REDIRECTS, **PAGE_REDIRECTS}
                if parts.scheme != 'https' or (parts.netloc, parts.path) != redirects.get(item['taskName']):
                    raise CheckinError('任务页跳转到了未接入的入口，未提交完成记录。', 'TASK_PAGE_CHANGED', 409)
            if page.status_code != 200:
                raise CheckinError(f'任务页面 HTTP {page.status_code}，未提交完成记录。', 'TASK_PAGE_FAILED')
        pending = {'kind': 'finish', 'id': task_id, 'month': month,
                   'date': self.scheduler.now().date().isoformat()}
        if kuaishou_task:
            if self.kuaishou_factory is None:
                raise CheckinError('当前账号尚未接入本人快手登录。', 'KUAISHOU_LOGIN_REQUIRED', 401)
            def prepare_kuaishou():
                client._current(snapshot)
                self._save(login_id, pending)
            result = self.kuaishou_factory(login_id).complete(item, info, before_submit=prepare_kuaishou)
            self._record(login_id, kuaishou={**result, 'date':pending['date']})
        elif cainiao_task:
            if self.cainiao_factory is None:
                raise CheckinError('当前账号尚未接入菜鸟兼容授权。','TAOBAO_LOGIN_REQUIRED',401)
            def prepare_cainiao():
                client._current(snapshot)
                self._save(login_id,pending)
            result = self.cainiao_factory(login_id).complete(item,info,before_submit=prepare_cainiao)
            self._record(login_id,cainiao=result)
        elif taobao_task:
            if self.taobao_factory is None:
                raise CheckinError('当前账号尚未接入淘宝授权。','TAOBAO_LOGIN_REQUIRED',401)
            def prepare_taobao():
                client._current(snapshot)
                self._save(login_id,pending)
            result = self.taobao_factory(login_id).complete(item,info,before_submit=prepare_taobao)
            self._record(login_id,taobao=result)
        elif baidu_map_task:
            if self.baidu_map_factory is None:
                raise CheckinError('当前账号尚未接入百度地图授权。', 'BAIDU_MAP_LOGIN_REQUIRED', 401)
            def prepare_baidu_map():
                client._current(snapshot)
                self._save(login_id, pending)
            result = self.baidu_map_factory(login_id).complete(item, info, before_submit=prepare_baidu_map)
            self._record(login_id, baidu_map=result)
        elif migu_task:
            if self.migu_factory is None:
                raise CheckinError('当前客户端未接入咪咕任务授权。', 'MIGU_LOGIN_REQUIRED', 401)
            def prepare_migu():
                client._current(snapshot)
                self._save(login_id, pending)
            result = self.migu_factory(login_id).complete(item, info, client, session, before_submit=prepare_migu)
            self._record(login_id, migu_login=result)
        elif item['taskName'] in VIDEO_TASKS:
            if self.video_factory is None:
                raise CheckinError('当前客户端未接入视频播放。', 'PLAYBACK_UNAVAILABLE')
            playback = self.video_factory(login_id).complete(item, info,
                before_submit=lambda: self._save(login_id, pending))
            self._record(login_id, video_playback=playback)
        elif item['taskName'] in CLOUD_TASKS:
            if self.cloud_factory is None:
                raise CheckinError('当前客户端未接入云盘任务授权。', 'CLOUD_LOGIN_REQUIRED', 401)
            browse = self.cloud_factory(login_id).complete(item, info,
                before_submit=lambda: self._save(login_id, pending))
            self._record(login_id, cloud_browse=browse)
        elif item['taskName'] in OPEN_TASKS:
            def prepare():
                client._current(snapshot)
                self._save(login_id, pending)
            complete_activity_browse(session, info, before_submit=prepare)
        elif item['taskName'] in QUERY_TASKS:
            self.query(login_id, QUERY_TASKS[item['taskName']][0])
        if not partner_task and item['taskName'] not in VIDEO_TASKS and item['taskName'] not in CLOUD_TASKS and item['taskName'] not in OPEN_TASKS:
            time.sleep(max(0, duration - (time.monotonic() - start)))
            random = '4xani51zw4aokw2tv73i206easvcntf2' + str(int(time.time() * 1000))
            body = {'taskId': task_id, 'taskType': info['taskType'], 'random': random,
                    'sign': hashlib.md5((task_id + '|' + random).encode()).hexdigest()}
            response = client._post(session, FINISH, body, snapshot=snapshot,
                                   before_submit=lambda: self._save(login_id, pending))
            _data(response, stage='查询任务完成')
        state = self._state(login_id, client, session)
        task = next((row for row in state['tasks'] if row['id'] == task_id), None)
        if task is None or task['state'] not in {'claimable', 'claimed'}:
            if kuaishou_task:
                raise CheckinError('快手已承接，移动官方回传尚待确认；将先查询结果，不重复提交。',
                                   'RESULT_UNCERTAIN', 409, uncertain=True)
            raise CheckinError('官方尚未确认任务完成，请先查询结果。', 'RESULT_UNCERTAIN', 409, uncertain=True)
        return task['state']

    def _perform_tasks(self, login_id):
        now = self.scheduler.now()
        today = now.date().isoformat()
        same_day = self.data.get(login_id, {}).get('date') == today
        attempts = self.data.get(login_id, {}).get('attempts', 0) if same_day else 0
        try:
            if not same_day:
                self._record(login_id, tasks=[], awaiting_awards={}, deferred_tasks={})
            self._record(login_id, date=today, state='running', checked_at=now.isoformat(),
                         retry_at=None, attempts=attempts + 1, code='', message='正在查询官方任务进度')
            client = self.scheduler.client_factory(lambda: self.scheduler.credentials(login_id))
            with client._session() as (session, snapshot), self._account_lock(login_id):
                state = self._state(login_id, client, session)
                if state['pending']:
                    raise CheckinError('上次提交结果待核实，已暂停自动任务。', 'RESULT_UNCERTAIN', 409, uncertain=True)
                if not state['tasks_available']:
                    raise CheckinError('官方当前未提供任务列表，等待服务恢复后继续。', 'UPSTREAM_TASKS_UNAVAILABLE', 503)
                initial_balance = state['balance']
                self._record(login_id, before=initial_balance, balance=initial_balance)
                tasks = _data(client._post(session, TASK_LIST))['tasks']
                deferred = {}
                for item in tasks:
                    if str(item['awardType']) not in {'1', '2'} or str(item['status']) == '2':
                        continue
                    try:
                        if str(item['status']) == '0':
                            if not _task_support(item)[0]:
                                continue
                            self._record(login_id, message='正在执行 ' + item['taskName'])
                            current = self._browse(login_id, client, session, snapshot, item, state['server_time'][:7])
                            if current == 'claimed':
                                continue
                        if str(item['status']) not in {'0', '1'}:
                            continue
                        result = self.run(login_id, kind='claim', item_id=str(item['taskId']))
                        self._record(login_id, balance=result['balance'])
                        if result['pending']:
                            raise CheckinError('领豆结果待官方确认，请刷新核对。', 'RESULT_UNCERTAIN', 409, uncertain=True)
                    except CheckinError as error:
                        if getattr(error,'definitive_rejection',False):
                            pending = self.pending.get(login_id,{})
                            if pending.get('kind')=='finish' and pending.get('id')==str(item['taskId']):
                                self._save(login_id,{})
                        if (error.code not in {'VIDEO_CONTENT_UNAVAILABLE', 'PLAYBACK_UNAVAILABLE', 'VIDEO_LOGIN_REQUIRED',
                                               'CLOUD_AUTH_REQUIRED', 'CLOUD_LOGIN_REQUIRED',
                                               'MIGU_LOGIN_REQUIRED',
                                               'BAIDU_MAP_LOGIN_REQUIRED', 'BAIDU_MAP_QUERY_REJECTED',
                                               'KUAISHOU_LOGIN_REQUIRED', 'KUAISHOU_QUERY_REJECTED', 'KUAISHOU_TASK_CHANGED',
                                               'TAOBAO_LOGIN_REQUIRED',
                                               'APP_LOGIN_REQUIRED', 'BILLS_LOGIN_REQUIRED', 'USAGE_LOGIN_REQUIRED',
                                               'SERVICES_LOGIN_REQUIRED',
                                               'TASK_CHANGED', 'TASK_PAGE_CHANGED'}
                                or error.uncertain or self.pending[login_id]):
                            raise
                        deferred[str(item['taskId'])] = {'code':error.code, 'message':str(error)}
                self._record(login_id, deferred_tasks=deferred)
                state = self._state(login_id, client, session)
                self._record(login_id, state='partial' if deferred else 'completed',
                             message=f'本次已执行可做任务，{len(deferred)} 项仍待处理' if deferred else '本次可执行任务已核对完成',
                             before=initial_balance, balance=state['balance'], tasks=state['tasks'],
                             checked_at=self.scheduler.now().isoformat())
        except (CheckinError, LoginStorageError) as error:
            temporary = getattr(error, 'code', '') in {
                'UPSTREAM_NETWORK', 'TASK_PAGE_FAILED', 'BILLS_NETWORK', 'USAGE_NETWORK', 'ITEM_UNAVAILABLE',
                'UPSTREAM_TASKS_UNAVAILABLE', 'UPSTREAM_PLUGIN_UNAVAILABLE'}
            # A retry starts by querying the durable marker. While the original
            # result is unknown _state keeps it, so no submission is replayed.
            temporary = temporary or bool(getattr(error, 'uncertain', False) and self.pending.get(login_id))
            retry = (self.scheduler.now() + timedelta(minutes=5)).isoformat() if temporary and attempts < 2 else None
            self._record(login_id, state='failed', message=str(error), code=getattr(error, 'code', ''),
                         retry_at=retry, checked_at=self.scheduler.now().isoformat())
        finally:
            with self.lock:
                self.running.discard(login_id)
