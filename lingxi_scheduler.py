"""Persisted, bounded Lingxi bursts. Each planned send has an independent stream."""
from __future__ import annotations

from copy import deepcopy
from collections import deque
from datetime import datetime,timedelta,timezone
import json
import math
import re
import secrets
import threading
import time

from login_storage import LoginStorageError

SHANGHAI=timezone(timedelta(hours=8))
ACTIVE={'preparing','running','queued','receiving','stopping'}
MAX_REPLY=24000


class TaskError(Exception):
    def __init__(self,message,status=400):
        super().__init__(message);self.status=status


def parse_time(value):
    try:
        result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return (result.replace(tzinfo=SHANGHAI) if result.tzinfo is None else result).astimezone(SHANGHAI)
    except (ValueError,TypeError,OverflowError) as error:
        raise TaskError('请选择有效的发送时间（北京时间）。') from error


def validate_plan(data,now):
    content=data.get('content')
    if not isinstance(content,str) or not 1<=len(content.strip())<=8000:
        raise TaskError('发送内容须为 1～8000 个字符。')
    count=data.get('repeat_count',3);interval=data.get('interval',1)
    if isinstance(count,bool) or not isinstance(count,int) or not 1<=count<=20:
        raise TaskError('发送次数须为 1～20 的整数。')
    if isinstance(interval,bool) or not isinstance(interval,(int,float)) or not math.isfinite(interval) or not .2<=interval<=3600:
        raise TaskError('发送间隔须为 0.2～3600 秒。')
    if (count-1)*interval>3600:
        raise TaskError('一轮发送的总时长不能超过 1 小时。')
    target=parse_time(data.get('run_at'))
    if target<=now or target>now+timedelta(days=366):
        raise TaskError('发送时间须晚于当前时间，且在一年内。')
    weekly=data.get('weekly',False)
    if not isinstance(weekly,bool):raise TaskError('重复方式无效。')
    return dict(content=content.strip(),repeat_count=count,interval=float(interval),
                run_at=target.isoformat(),next_run_at=target.isoformat(),weekly=weekly)


class LingxiScheduler:
    def __init__(self,prepare,send,account_info,*,storage=None,now=None):
        self.prepare,self.send,self.account_info=prepare,send,account_info
        self.storage=storage;self.now=now or (lambda:datetime.now(SHANGHAI))
        self.lock=threading.RLock();self.jobs={};self.workers={};self.cancellations={}
        self.shutdown_event=threading.Event();self.thread=None;self.warning='';self.load_error=''
        self.condition=threading.Condition(self.lock)
        self.concurrency=256;self.running_requests=0;self.pending=deque();self.dispatcher=None
        if storage is not None:
            try:
                configured=storage.load_lingxi_concurrency()
                if configured is not None:self.concurrency=configured
                for job in storage.load_lingxi_tasks():
                    if (not re.fullmatch('[a-f0-9]{32}',job.get('id',''))
                        or not isinstance(job.get('login_id'),str)
                        or not 1<=job.get('repeat_count',0)<=20
                        or not isinstance(job.get('content'),str)
                        or not isinstance(job.get('attempts',[]),list)):
                        raise ValueError
                    parse_time(job['next_run_at'] or job['run_at'])
                    if job.get('state') in ACTIVE:
                        for attempt in job.get('attempts',[]):
                            if attempt['status'] in {'planned','queued','receiving'}:
                                attempt['status']='interrupted';attempt['error']='服务中断，未自动重发'
                        self._finish_locked(job,'interrupted','上次执行被中断，未补发')
                    self.jobs[job['id']]=job
            except (LoginStorageError,ValueError,KeyError,TypeError,TaskError) as error:
                self.jobs={};self.load_error=self.warning='灵犀任务读取失败，已保留原数据；请检查数据库。'

    def _persist_locked(self,job):
        if self.storage is None:return
        try:
            self.storage.save_lingxi_tasks([job]);self.warning=''
        except LoginStorageError as error:
            self.warning=str(error);raise TaskError(str(error),503) from error

    def snapshots(self,detail_id=None):
        with self.lock:
            values=[]
            for job in self.jobs.values():
                if detail_id is not None and job['id']!=detail_id:continue
                previous=[attempt for run in job.get('history',[]) for attempt in run['attempts']]
                valid=[attempt for attempt in [*previous,*job['attempts']] if attempt['status']=='success'
                       and (attempt.get('text','').strip() or attempt.get('cards'))]
                latest=max(valid,key=lambda attempt:attempt.get('finished_at') or attempt.get('sent_at') or '',default=None)
                last_reply=None
                if latest:
                    last_reply={'text':(latest.get('text','').strip() or '已收到卡片回复，点击“查看回复”展开。')[:500],
                                'index':latest['index'],'finished_at':latest.get('finished_at')}
                if detail_id is not None:values.append(deepcopy({**job,'last_reply':last_reply}));continue
                values.append(deepcopy({**job,'last_reply':last_reply,'content':job['content'][:160],'history':[],
                    'attempts':[{k:v for k,v in a.items() if k not in {'text','cards'}} for a in job['attempts']]}))
        result=[]
        for job in values:
            info=self.account_info(job['login_id']) or {}
            job['account_name']=info.get('name') or info.get('phone_masked') or '已退出账号'
            job['phone_masked']=info.get('phone_masked','')
            job['account_status']=info.get('status','missing')
            job['sent_count']=sum(bool(a.get('sent_at')) for a in job['attempts'])
            job['success_count']=sum(a['status']=='success' for a in job['attempts'])
            job['queued_count']=sum(a['status']=='queued' for a in job['attempts'])
            result.append(job)
        return sorted(result,key=lambda j:(not j['enabled'],j.get('next_run_at') or j['run_at'],j['id']))

    def settings(self):
        with self.lock:
            return {'concurrency':self.concurrency,'running':self.running_requests,'queued':len(self.pending)}

    def update_settings(self,data):
        value=data.get('concurrency')
        if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=1024:
            raise TaskError('并发上限须为 1～1024 的整数。')
        with self.condition:
            if self.storage is not None:
                try:self.storage.save_lingxi_concurrency(value)
                except LoginStorageError as error:raise TaskError(str(error),503) from error
            self.concurrency=value;self.condition.notify_all()
        return self.settings()

    def save(self,data):
        if self.load_error:raise TaskError(self.load_error,503)
        plan=validate_plan(data,self.now());ids=data.get('login_ids')
        if not isinstance(ids,list) or not ids or any(not isinstance(i,str) or not i for i in ids):
            raise TaskError('请至少选择一个共享账号。')
        ids=list(dict.fromkeys(ids))
        for login_id in ids:
            info=self.account_info(login_id)
            if not info or info.get('status')=='login_required':raise TaskError('所选账号需要登录，请先打开账号管理。')
        edit_id=data.get('id')
        with self.lock:
            if edit_id:
                if len(ids)!=1 or edit_id not in self.jobs:raise TaskError('编辑时请选择一个账号和有效任务。')
                if edit_id in self.workers:raise TaskError('任务正在执行，请先停止并等待回复结束。',409)
            elif len(self.jobs)+len(ids)>50:raise TaskError('最多保留 50 项任务，请先删除已结束的任务。')
            stamp=self.now().isoformat();new=[]
            for login_id in ids:
                previous=self.jobs.get(edit_id,{})
                job={**plan,'id':edit_id or secrets.token_hex(16),'login_id':login_id,'enabled':True,
                     'state':'waiting','message':'等待预约时间','attempts':[],
                     'created_at':previous.get('created_at',stamp),'updated_at':stamp,'history':[]}
                new.append(job)
            if self.storage is not None:
                try:self.storage.save_lingxi_tasks(new)
                except LoginStorageError as error:raise TaskError(str(error),503) from error
            for job in new:self.jobs[job['id']]=job
        return [job['id'] for job in new]

    def adjust_batch(self,data):
        if self.load_error:raise TaskError(self.load_error,503)
        ids=data.get('ids');changes=data.get('changes')
        if not isinstance(ids,list) or not ids or any(not isinstance(key,str) or not key for key in ids):
            raise TaskError('请勾选要调整的任务。')
        if not isinstance(changes,dict) or not changes or changes.keys()-{'content','run_at','repeat_count','interval','weekly'}:
            raise TaskError('请选择要修改的内容、时间或发送设置。')
        ids=list(dict.fromkeys(ids));now=self.now();updated=[]
        with self.lock:
            for task_id in ids:
                job=self.jobs.get(task_id)
                if job is None:raise TaskError('所选任务已被删除，请刷新列表。',404)
                if not job['enabled'] or job['state']!='waiting' or task_id in self.workers:
                    raise TaskError('仅支持批量调整待执行任务，请刷新后重新选择。',409)
                plan=validate_plan({**job,'run_at':job['next_run_at'],**changes},now)
                item={**job,**{key:plan[key] for key in changes},'updated_at':now.isoformat(),
                      'message':'预约已批量调整，等待执行'}
                if 'run_at' in changes:item['next_run_at']=plan['run_at']
                updated.append(item)
            if self.storage is not None:
                try:self.storage.save_lingxi_tasks(updated)
                except LoginStorageError as error:raise TaskError(str(error),503) from error
            for job in updated:self.jobs[job['id']]=job
        return ids

    def stop(self,task_id):
        self.stop_batch({'ids':[task_id]})

    def stop_batch(self,data):
        ids=data.get('ids') if isinstance(data,dict) else None
        if not isinstance(ids,list) or not ids or any(not isinstance(key,str) or not key for key in ids):
            raise TaskError('请勾选要停止的任务。')
        ids=list(dict.fromkeys(ids));stopped=[]
        with self.condition:
            jobs=[]
            for task_id in ids:
                job=self.jobs.get(task_id)
                if job is None:raise TaskError('所选任务已不存在，请刷新列表。',404)
                jobs.append(job)
            for job in jobs:
                task_id=job['id']
                if not job['enabled'] and job['state'] not in ACTIVE and task_id not in self.workers:continue
                job['enabled']=False
                if task_id in self.cancellations:self.cancellations[task_id].set()
                for attempt in job['attempts']:
                    if attempt['status'] in {'planned','queued'}:
                        attempt['status']='cancelled';attempt['error']='已停止，本次未发送'
                remaining=deque()
                for item in self.pending:
                    if item[0]['id']==task_id:item[-1].set()
                    else:remaining.append(item)
                self.pending=remaining;self.condition.notify_all()
                job['state']='stopping' if task_id in self.workers else 'cancelled'
                job['message']='已停止后续发送，正在接收的回复将保留' if task_id in self.workers else '已停止'
                job['updated_at']=self.now().isoformat();self._persist_locked(job);stopped.append(task_id)
        return stopped

    def cancel_login(self,login_id):
        with self.lock:ids=[j['id'] for j in self.jobs.values() if j['login_id']==login_id and j['enabled']]
        for task_id in ids:self.stop(task_id)

    def delete(self,task_id):
        self.delete_batch({'ids':[task_id]})

    def delete_batch(self,data):
        if self.load_error:raise TaskError(self.load_error,503)
        ids=data.get('ids') if isinstance(data,dict) else None
        if not isinstance(ids,list) or not ids or any(not isinstance(key,str) or not key for key in ids):
            raise TaskError('请勾选要删除的任务。')
        ids=list(dict.fromkeys(ids))
        with self.lock:
            for task_id in ids:
                job=self.jobs.get(task_id)
                if job is None:raise TaskError('所选任务已不存在，请刷新列表。',404)
                if task_id in self.workers or job['state'] in ACTIVE:
                    raise TaskError('所选任务正在执行，请先停止并等待结束后删除。',409)
            if self.storage is not None:
                try:self.storage.delete_lingxi_tasks(ids)
                except LoginStorageError as error:raise TaskError(str(error),503) from error
            for task_id in ids:self.jobs.pop(task_id)
        return ids

    def busy_login(self,login_id):
        with self.lock:return any(self.jobs[i]['login_id']==login_id for i in self.workers)

    def _finish_locked(self,job,state,message):
        job['state']=state;job['message']=message;job['last_state']=state
        job['finished_at']=self.now().isoformat();job['updated_at']=job['finished_at']
        if job.get('weekly') and job.get('enabled'):
            target=parse_time(job['next_run_at'] or job['run_at'])+timedelta(days=7)
            while target<=self.now():target+=timedelta(days=7)
            job['next_run_at']=target.isoformat();job['state']='waiting'
            job['message']=message+'；等待下周执行'
        else:job['enabled']=False
        self._persist_locked(job)

    def tick(self):
        with self.lock:
            for job in list(self.jobs.values()):
                if not job['enabled'] or job['id'] in self.workers:continue
                target=parse_time(job['next_run_at'])
                if target<self.now()-timedelta(seconds=2):
                    self._finish_locked(job,'missed','已错过预约时间，未补发');continue
                if target-self.now()>timedelta(seconds=30):continue
                cancel=threading.Event();self.cancellations[job['id']]=cancel
                job['state']='preparing';job['message']='正在校验共享账号';job['updated_at']=self.now().isoformat()
                self._persist_locked(job)
                worker=threading.Thread(target=self._worker,args=(job['id'],target,cancel),daemon=True,
                                        name='lingxi-task-'+job['id'][:6])
                self.workers[job['id']]=worker;worker.start()

    def _wait_until(self,target,cancel):
        while not cancel.is_set() and not self.shutdown_event.is_set():
            remaining=(target-self.now()).total_seconds()
            if remaining<=0:return True
            cancel.wait(min(remaining,.2))
        return False

    def _worker(self,task_id,target,cancel):
        sends=[]
        try:
            with self.lock:
                job=self.jobs[task_id];login_id=job['login_id'];content=job['content']
                if job['attempts']:
                    job['history']=(job.get('history',[])+[{'run_at':job.get('active_run_at'),
                        'state':job.get('last_state'),'attempts':deepcopy(job['attempts'])}])[-2:]
                job['active_run_at']=target.isoformat()
                job['attempts']=[{'index':i+1,'planned_at':(target+timedelta(seconds=i*job['interval'])).isoformat(),
                    'sent_at':None,'status':'planned','text':'','cards':[],'error':''} for i in range(job['repeat_count'])]
                self._persist_locked(job)
            if cancel.is_set() or self.shutdown_event.is_set():raise TaskError('任务已停止')
            self.prepare(login_id)
            if (self.now()-target).total_seconds()>2:raise TaskError('账号准备已超过预约时间，本轮未发送')
            with self.lock:
                job['state']='running';job['message']='账号已就绪，等待预约时间';self._persist_locked(job)
            for attempt in job['attempts']:
                due=parse_time(attempt['planned_at'])
                if not self._wait_until(due,cancel):break
                with self.condition:
                    if cancel.is_set() or self.shutdown_event.is_set():break
                    done=threading.Event();attempt['status']='queued';attempt['queued_at']=self.now().isoformat()
                    self._persist_locked(job)
                    self.pending.append((job,attempt,login_id,content,cancel,done));sends.append(done)
                    if self.dispatcher is None:
                        self.dispatcher=threading.Thread(target=self._dispatch,daemon=True,name='lingxi-send-queue')
                        self.dispatcher.start()
                    self.condition.notify_all()
            with self.lock:
                if not cancel.is_set():
                    job['state']='receiving' if any(a['status']=='receiving' for a in job['attempts']) else 'queued'
                    job['message']='发送计划已入队，等待发送或接收回复';self._persist_locked(job)
        except Exception as error:
            with self.lock:
                job=self.jobs[task_id]
                # Provider errors are already sanitized by the app boundary.
                job['message']=str(error) if isinstance(error,TaskError) else '账号准备失败，本轮未发送，请校验账号后重新预约'
                for attempt in job.get('attempts',[]):
                    if attempt['status']=='planned':attempt['status']='failed';attempt['error']=job['message']
        finally:
            for done in sends:done.wait()
            with self.lock:
                job=self.jobs[task_id]
                for attempt in job.get('attempts',[]):
                    if attempt['status'] in {'planned','queued'}:attempt['status']='cancelled';attempt['error']='未发送'
                succeeded=sum(a['status']=='success' for a in job.get('attempts',[]))
                state='cancelled' if cancel.is_set() else 'completed' if succeeded==job['repeat_count'] else 'partial' if succeeded else 'failed'
                if self.shutdown_event.is_set():state='interrupted';job['enabled']=False
                try:self._finish_locked(job,state,f'本轮收到 {succeeded}/{job["repeat_count"]} 次完整回复')
                finally:self.workers.pop(task_id,None);self.cancellations.pop(task_id,None)

    def _dispatch(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda:self.shutdown_event.is_set() or self.pending and self.running_requests<self.concurrency)
                if self.shutdown_event.is_set():
                    while self.pending:
                        job,attempt,_,_,_,done=self.pending.popleft()
                        attempt['status']='interrupted';attempt['error']='服务停止，排队请求未发送'
                        done.set()
                    self.condition.notify_all()
                    return
                item=self.pending.popleft();self.running_requests+=1;self.condition.notify_all()
            threading.Thread(target=self._send_one,args=item,daemon=True).start()

    def _send_one(self,job,attempt,login_id,content,cancel,done):
        stream=None;started=time.monotonic();received=False
        try:
            def on_sent():
                with self.lock:
                    if cancel.is_set() or self.shutdown_event.is_set():raise TaskError('已停止，本次未发送')
                    attempt['status']='receiving';attempt['sent_at']=self.now().isoformat()
                    attempt['queue_wait_seconds']=round((parse_time(attempt['sent_at'])-parse_time(attempt['queued_at'])).total_seconds(),3)
                    job['state']='receiving'
                    job['message']=f'第 {attempt["index"]} 次已发起，正在接收回复';job['updated_at']=self.now().isoformat()
                    try:self._persist_locked(job)
                    except TaskError:
                        attempt['sent_at']=None;raise
            stream=self.send(login_id,content,on_sent)
            for event in stream:
                if time.monotonic()-started>180:raise TaskError('回复接收超时，已保留收到的内容')
                with self.lock:
                    kind=event.get('type')
                    if kind=='delta':
                        text=str(event.get('text',''));attempt['text']=(attempt['text']+text)[:MAX_REPLY]
                        if len(attempt['text'])==MAX_REPLY:attempt['truncated']=True
                    elif kind=='clear':attempt['text']='';attempt['cards']=[]
                    elif kind=='card' and len(attempt['cards'])<4:
                        attempt['cards'].append(json.dumps(event.get('content'),ensure_ascii=False)[:2000])
                    elif kind=='done':received=True
                    elif kind=='error':raise TaskError('灵犀返回错误，未自动重发')
            with self.lock:
                attempt['status']='success' if received else 'failed'
                if not received:attempt['error']='未收到完整回复，未自动重发'
        except Exception as error:
            with self.lock:
                attempt['status']='cancelled' if cancel.is_set() and not attempt.get('sent_at') else 'failed'
                attempt['error']=str(error) if isinstance(error,TaskError) else '发送或接收失败，已保留收到的内容；未自动重发'
        finally:
            if stream is not None and hasattr(stream,'close'):
                try:stream.close()
                except Exception:
                    with self.lock:
                        attempt['status']='failed';attempt['error']='回复连接关闭异常，已保留收到的内容'
            with self.condition:
                attempt['finished_at']=self.now().isoformat();job['updated_at']=attempt['finished_at']
                try:self._persist_locked(job)
                finally:
                    self.running_requests-=1;self.condition.notify_all();done.set()

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.thread=threading.Thread(target=self._loop,daemon=True,name='lingxi-scheduler');self.thread.start()

    def _loop(self):
        while not self.shutdown_event.is_set():
            try:self.tick()
            except Exception:self.warning='任务维护暂时失败，请检查本地服务和数据库。'
            self.shutdown_event.wait(.2)

    def shutdown(self):
        with self.condition:
            self.shutdown_event.set();self.condition.notify_all()
