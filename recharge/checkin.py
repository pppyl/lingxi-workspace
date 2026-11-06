"""Wednesday recharge's monthly seven-day streak, separate from its weekly draw."""
from copy import deepcopy
from datetime import datetime, timedelta
import logging
import re
from threading import RLock
from urllib.parse import urlsplit

from feature_runtime import ThreadPoolExecutor, feature_running
from http_transport import RequestException
from .cmcc import ActivityClient, ActivityError, SHANGHAI, public_prize


def sign_state(data, now):
    if not isinstance(data,dict) or str(data.get('isTodaySign')) not in {'0','1'} or not isinstance(data.get('taskList'),list):
        raise ActivityError('官方打卡进度格式异常，请稍后查询。','SIGN_FORMAT',502)
    try:
        streak=int(data.get('signTimes') or 0); rd=int(data['rdStatus'])
        tasks={int(task['order']):task for task in data['taskList']}
        if not 0<=streak<=7:raise ValueError
    except (ValueError,TypeError,KeyError) as error:
        raise ActivityError('官方打卡任务格式异常，本次没有提交。','SIGN_FORMAT',502) from error
    today=str(data['isTodaySign'])=='1';broken=str(data.get('isBroken'))=='1'
    day=1 if broken else min(streak+(not today),7)
    task=tasks.get(day,{})
    task_id=task.get('taskId','')
    received=rd in {3,6,7}
    ended=str(data.get('isEndMonth'))=='1'
    automatic=task_id=='llym' or task_id==f'ljdk{day}' and day in {2,4,6,7}
    action='claim' if rd==2 and streak==7 else 'done' if received or today else 'closed' if ended or rd==5 else 'sign' if automatic else 'manual'
    labels={'claim':'可领取8折券','done':'本月已领取' if received else '今日已打卡',
            'closed':'本月已抢光' if rd==5 else '本月剩余天数不足','sign':'今日待打卡',
            'manual':'待邀请' if task_id=='yqyh' else '待充值' if task_id=='czhf' else '任务需人工完成'}
    message=task.get('task') or task.get('title') or '请在中国移动 APP 核对当日任务。'
    if action=='done':message='本月已领取8折话费券。' if received else '今日打卡已确认，明天继续。'
    if action=='manual':message+= '；请在官方 APP 完成后点击“查询进度”。'
    return {'date':now.date().isoformat(),'month':now.strftime('%Y-%m'),'today_signed':today,
            'streak':streak,'day':day,'broken':broken,'received':received,'rd_status':rd,
            'action':action,'status':labels[action],'message':message,'task_id':task_id,
            'task':task.get('task') or task.get('title') or '', 'prize':public_prize(data.get('prizeInfo')),
            'queried_at':now.isoformat(),'jump_url':task.get('jumpUrl') if task_id=='llym' else None}


class ContinuousCheckin:
    def __init__(self, accounts, ensure_account, *, storage=None, now=None, shutdown=None, weekly_busy=None):
        self.accounts,self.ensure_account,self.storage=accounts,ensure_account,storage
        self.now=now or (lambda:datetime.now(SHANGHAI))
        self.shutdown=shutdown
        self.weekly_busy=weekly_busy or (lambda account_id:False)
        self.lock,self.active=RLock(),set()
        self.pool=ThreadPoolExecutor(max_workers=4,thread_name_prefix='wed-checkin')
        self.data=(storage.load_activity_data('wed-checkin') if storage else None) or {
            'settings':{'enabled':True,'time':'00:01'},'records':{}}
        self.next_scan=None

    def _save(self):
        if self.storage:self.storage.save_activity_data('wed-checkin',self.data)

    def forget(self, login_id):
        with self.lock:
            self.data['records'].pop(login_id,None);self._save()

    def snapshots(self):
        known={a['id'] for a in self.accounts()}
        with self.lock:
            removed=set(self.data['records'])-known
            for login_id in removed:self.data['records'].pop(login_id)
            if removed:self._save()
            result=deepcopy(self.data)
            result['active']=list(self.active)
            return result

    def configure(self, data):
        if type(data.get('enabled')) is not bool or not isinstance(data.get('time'),str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',data['time']):
            raise ActivityError('请选择是否自动打卡，并填写有效时间。','INVALID_SIGN_SETTINGS')
        with self.lock:
            self.data['settings']={'enabled':data['enabled'],'time':data['time']};self._save();self.next_scan=None
        return self.snapshots()

    def _update(self, login_id, **fields):
        with self.lock:
            record=self.data['records'].setdefault(login_id,{})
            record.update(fields)
            self._save()

    def tick(self):
        now=self.now()
        if self.next_scan and now<self.next_scan:return
        self.next_scan=now+timedelta(seconds=10)
        snapshot=self.snapshots();settings=snapshot['settings']
        if not settings['enabled'] or not feature_running():return
        hour,minute=map(int,settings['time'].split(':'))
        if now<now.replace(hour=hour,minute=minute,second=0,microsecond=0):return
        for login in self.accounts():
            if login['status']!='active':continue
            record=snapshot['records'].get(login['id'],{});state=record.get('progress',{})
            if state.get('month')==now.strftime('%Y-%m') and (state.get('received') or state.get('action')=='closed'):continue
            if state.get('date')==now.date().isoformat() and state.get('action') in {'done','manual'}:continue
            if record.get('attempt_date')==now.date().isoformat():
                recovered=(record.get('error_code') in {'APP_LOGIN_REQUIRED','LOGIN_REQUIRED','GOTO_LOGIN'}
                           and record.get('attempt_revision')!=login.get('credential_revision'))
                if not recovered:
                    if record.get('attempts',0)>=5 or record.get('blocked'):continue
                    if record.get('next_at') and now<datetime.fromisoformat(record['next_at']):continue
            account=self.ensure_account(login)
            if self.weekly_busy(account.id):continue
            with self.lock:
                if len(self.active)>=4:break
                if login['id'] in self.active:continue
                self.active.add(login['id'])
            self.pool.submit(self._execute,login,account,True,automatic=True)

    def run(self, login_id, *, submit=False):
        login=next((a for a in self.accounts() if a['id']==login_id),None)
        if login is None:raise ActivityError('账号已退出，请重新选择。','ACCOUNT_NOT_FOUND',404)
        account=self.ensure_account(login)
        if self.weekly_busy(account.id):raise ActivityError('周三抢券正在运行，请稍后再查询打卡。','JOB_RUNNING',409)
        with self.lock:
            if login_id in self.active:raise ActivityError('此账号正在处理打卡，请稍候。','SIGN_BUSY',409)
            self.active.add(login_id)
        return self._execute(login,account,submit)

    def _execute(self, login, account, submit, *, automatic=False):
        login_id=login['id'];now=self.now();day=now.date().isoformat()
        try:
            with account.client.operation_lock:
                if not feature_running():raise ActivityError('周三充值日已关闭。','FEATURE_DISABLED',409)
                client=account.client
                if client.mode=='browser':raise ActivityError('连续打卡需使用共享 APP 账号。','APP_LOGIN_REQUIRED',401)
                value=client.read('querySignActInfo')
                progress=sign_state(value.get('data'),self.now())
                with self.lock:
                    record=self.data['records'].get(login_id,{})
                    pending=record.get('pending')
                if pending and (pending['kind']=='sign' and (pending['date']!=day or progress['today_signed'])
                                or pending['kind']=='claim' and (pending['month']!=progress['month'] or progress['received'])):
                    pending=None
                self._update(login_id,progress=progress,pending=pending,message=progress['message'],state='ready')
                if pending:
                    raise ActivityError('上次提交结果待核实，已查询官方进度；不会重复提交。','SIGN_UNCERTAIN',409)
                if submit and progress['action'] in {'sign','claim'}:
                    if automatic and not self.data['settings']['enabled']:return progress
                    if progress['task_id']=='llym' and progress['action']=='sign':
                        url=progress['jump_url'] or 'https://wx.10086.cn/qwhdhub/qwhdmark/1021122301'
                        parts=urlsplit(url)
                        if parts.scheme!='https' or parts.netloc!='wx.10086.cn' or parts.path!='/qwhdhub/qwhdmark/1021122301':
                            raise ActivityError('官方浏览任务地址已变化，请先核对新任务。','SIGN_TASK_CHANGED',409)
                        ActivityClient._follow(client.http,url)
                        if self.shutdown is not None:
                            if self.shutdown.wait(5):raise ActivityError('服务正在停止，尚未提交打卡。','SIGN_STOPPED',409)
                        else:
                            import time
                            time.sleep(5)
                    if not feature_running():raise ActivityError('周三充值日已关闭，尚未提交打卡。','FEATURE_DISABLED',409)
                    if automatic and not self.data['settings']['enabled']:return progress
                    kind='claim' if progress['action']=='claim' else 'sign'
                    marker={'kind':kind,'date':day,'month':progress['month'],'task_id':progress['task_id']}
                    self._update(login_id,pending=marker)
                    result=client.call('receiveSign' if kind=='claim' else 'signFinishTask',{} if kind=='claim' else {'taskId':progress['task_id']})
                    if str(result.get('code','UNKNOWN')) not in {'0','UNKNOWN'}:
                        self._update(login_id,pending=None)
                    client.check_result(result)
                    progress=sign_state(client.read('querySignActInfo').get('data'),self.now())
                    confirmed=progress['received'] if kind=='claim' else progress['today_signed']
                    self._update(login_id,progress=progress,pending=None if confirmed else marker)
                    if not confirmed:raise ActivityError('官方已接收请求，结果待核实；不会重复提交。','SIGN_UNCERTAIN',409)
                    if kind=='sign' and progress['action']=='claim':
                        if automatic and not self.data['settings']['enabled']:return progress
                        # The seventh check-in is confirmed; claiming is a separate one-shot operation.
                        self._update(login_id,pending={**marker,'kind':'claim'})
                        result=client.call('receiveSign',{})
                        if str(result.get('code','UNKNOWN')) not in {'0','UNKNOWN'}:self._update(login_id,pending=None)
                        client.check_result(result)
                        progress=sign_state(client.read('querySignActInfo').get('data'),self.now())
                        self._update(login_id,progress=progress)
                        if not progress['received']:raise ActivityError('领奖结果待核实，稍后查询确认；不会重复提交。','SIGN_UNCERTAIN',409)
                        self._update(login_id,pending=None)
                if client.credential_cache is not None:client.credential_cache.sync(client.http)
                tomorrow=now.replace(hour=int(self.data['settings']['time'][:2]),minute=int(self.data['settings']['time'][3:]),second=0,microsecond=0)+timedelta(days=1)
                next_at=now if not submit and progress['action'] in {'sign','claim'} else tomorrow
                self._update(login_id,message=progress['message'],state='ready',error_code=None,attempt_date=day,attempts=0,blocked=False,next_at=next_at.isoformat())
                return progress
        except (ActivityError,RequestException) as error:
            if not isinstance(error,ActivityError):error=ActivityError('连接官方打卡接口失败，请稍后查询核对。','UPSTREAM_NETWORK',502)
            with self.lock:
                previous=self.data['records'].get(login_id,{})
                attempts=(previous.get('attempts',0) if previous.get('attempt_date')==day else 0)+1
            # Definitive restrictions and authentication failures are not repeatedly retried.
            blocked=error.status in {401,403,429} or error.code in {'-800','8053','160019','160030','HTTP_ACCESS_LIMIT','APP_SSO_FAILED','SIGN_TASK_CHANGED'} or any(t in str(error) for t in ('不符合','无参与资格','不可参与'))
            self._update(login_id,message=str(error),state='failed',error_code=error.code,attempt_date=day,attempts=attempts,
                         attempt_revision=login.get('credential_revision'),blocked=blocked,next_at=(now+timedelta(minutes=5)).isoformat())
            logging.getLogger(__name__).info('周三连续打卡未完成：%s',error.code)
            if submit:return {'error':str(error),'code':error.code}
            raise error
        finally:
            with self.lock:self.active.discard(login_id)
