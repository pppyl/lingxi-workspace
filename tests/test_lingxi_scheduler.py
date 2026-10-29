from datetime import datetime,timedelta
from contextlib import closing
import json
import sqlite3
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch

from cryptography.fernet import Fernet

from app import create_app
from lingxi_scheduler import LingxiScheduler,TaskError,SHANGHAI,validate_plan
from login_storage import DatabaseLoginStorage,LoginStorageError


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.prepare=Mock()
        self.info=lambda key:{'name':'模拟账号','phone_masked':'199****0001','status':'active'} if key=='a'*32 else None
        self.scheduler=LingxiScheduler(self.prepare,self.sender,self.info)
        self.addCleanup(self.scheduler.shutdown)

    def sender(self,login_id,content,on_sent):
        on_sent();yield {'type':'delta','text':'模拟回复'};yield {'type':'done'}

    def plan(self,**changes):
        return {'login_ids':['a'*32],'content':'unit-test message',
                'run_at':(datetime.now(SHANGHAI)+timedelta(seconds=.15)).isoformat(),
                'repeat_count':1,'interval':.2,**changes}

    def finish(self,scheduler=None,timeout=5):
        scheduler=scheduler or self.scheduler
        deadline=time.monotonic()+timeout
        while scheduler.workers and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(scheduler.workers,'task should finish')

    def test_validation_defaults_and_beijing_timezone(self):
        now=datetime(2026,10,29,tzinfo=SHANGHAI)
        value=validate_plan({'content':' hello ','run_at':'2026-11-04T08:00:00'},now)
        self.assertEqual(value['repeat_count'],3);self.assertEqual(value['interval'],1)
        self.assertEqual(value['run_at'],'2026-11-04T08:00:00+08:00')
        for change in [{'repeat_count':True},{'repeat_count':0},{'repeat_count':21},
                       {'interval':float('nan')},{'interval':False},{'interval':.1},
                       {'weekly':'yes'},{'run_at':'2026-01-01'},{'content':' '}]:
            with self.subTest(change=change),self.assertRaises(TaskError):
                validate_plan({'content':'hello','run_at':'2026-11-04T08:00:00',**change},now)

    def test_three_sends_one_second_apart_do_not_wait_for_replies(self):
        times=[];all_started=threading.Event()
        def send(login_id,content,on_sent):
            on_sent();times.append(time.monotonic())
            if len(times)==3:all_started.set()
            yield {'type':'delta','text':'partial'}
            if not all_started.wait(3):raise TaskError('overlapping requests were blocked')
            yield {'type':'done'}
        self.scheduler.send=send
        task_id=self.scheduler.save(self.plan(repeat_count=3,interval=1))[0]
        start=time.monotonic();self.scheduler.tick();self.scheduler.tick();self.finish()
        self.assertEqual(len(times),3)
        self.assertGreaterEqual(times[0]-start,.10)
        for left,right in zip(times,times[1:]):self.assertAlmostEqual(right-left,1,delta=.25)
        job=self.scheduler.snapshots(task_id)[0]
        self.assertEqual(job['state'],'completed');self.assertEqual(job['sent_count'],3)
        self.assertEqual(job['success_count'],3);self.prepare.assert_called_once()

    def test_stop_cancels_unsent_requests_but_keeps_inflight_reply(self):
        sent=threading.Event();release=threading.Event();calls=[]
        def send(login_id,content,on_sent):
            on_sent();calls.append(content);sent.set();release.wait(2)
            yield {'type':'delta','text':'finished reply'};yield {'type':'done'}
        self.scheduler.send=send
        task_id=self.scheduler.save(self.plan(repeat_count=3))[0];self.scheduler.tick()
        self.assertTrue(sent.wait(1));self.scheduler.stop(task_id);release.set();self.finish()
        job=self.scheduler.snapshots(task_id)[0]
        self.assertEqual(len(calls),1);self.assertEqual(job['state'],'cancelled')
        self.assertEqual(job['attempts'][0]['text'],'finished reply')
        self.assertTrue(all(a['status']=='cancelled' for a in job['attempts'][1:]))

    def test_missed_job_is_not_replayed(self):
        task_id=self.scheduler.save(self.plan())[0]
        self.scheduler.now=lambda:datetime.now(SHANGHAI)+timedelta(minutes=1)
        self.scheduler.send=Mock();self.scheduler.tick()
        self.assertEqual(self.scheduler.jobs[task_id]['state'],'missed');self.scheduler.send.assert_not_called()

    def test_weekly_task_moves_to_next_week_after_run(self):
        task_id=self.scheduler.save(self.plan(weekly=True))[0]
        first=self.scheduler.jobs[task_id]['next_run_at'];self.scheduler.tick();self.finish()
        job=self.scheduler.jobs[task_id]
        self.assertTrue(job['enabled']);self.assertEqual(job['state'],'waiting')
        self.assertEqual(datetime.fromisoformat(job['next_run_at'])-datetime.fromisoformat(first),timedelta(days=7))

    def test_preparation_failure_does_not_send_or_automatically_retry(self):
        self.prepare.side_effect=TaskError('账号需验证')
        self.scheduler.send=Mock();task_id=self.scheduler.save(self.plan())[0]
        self.scheduler.tick();self.finish();self.scheduler.tick()
        self.scheduler.send.assert_not_called();self.assertFalse(self.scheduler.jobs[task_id]['enabled'])

    def test_partial_reply_and_failure_are_saved_without_replay(self):
        def send(login_id,content,on_sent):
            on_sent();yield {'type':'delta','text':'kept'};raise TaskError('network interrupted')
        self.scheduler.send=send;task_id=self.scheduler.save(self.plan())[0]
        self.scheduler.tick();self.finish();job=self.scheduler.snapshots(task_id)[0]
        self.assertEqual(job['attempts'][0]['text'],'kept');self.assertEqual(job['state'],'failed')
        self.assertEqual(job['sent_count'],1)

    def test_clear_and_cards_are_preserved(self):
        def send(login_id,content,on_sent):
            on_sent();yield {'type':'delta','text':'old'};yield {'type':'clear'}
            yield {'type':'delta','text':'new'};yield {'type':'card','content':{'title':'result'}};yield {'type':'done'}
        self.scheduler.send=send;task_id=self.scheduler.save(self.plan())[0]
        self.scheduler.tick();self.finish();attempt=self.scheduler.snapshots(task_id)[0]['attempts'][0]
        self.assertEqual(attempt['text'],'new');self.assertEqual(json.loads(attempt['cards'][0]),{'title':'result'})
        self.assertNotIn('text',self.scheduler.snapshots()[0]['attempts'][0])

    def test_edit_and_delete_do_not_create_extra_jobs(self):
        task_id=self.scheduler.save(self.plan())[0]
        self.scheduler.save(self.plan(id=task_id,content='updated'))
        self.assertEqual(len(self.scheduler.jobs),1);self.assertEqual(self.scheduler.jobs[task_id]['content'],'updated')
        self.scheduler.delete(task_id);self.assertFalse(self.scheduler.jobs)

    def test_batch_delete_preserves_unselected_tasks_and_deduplicates_ids(self):
        one,two,keep=[self.scheduler.save(self.plan(content=content))[0] for content in ['one','two','keep']]
        self.scheduler.jobs[one].update(state='completed',enabled=False)
        self.scheduler.jobs[two].update(state='partial',enabled=False)
        deleted=self.scheduler.delete_batch({'ids':[one,two,one]})
        self.assertEqual(deleted,[one,two]);self.assertEqual(list(self.scheduler.jobs),[keep])

    def test_batch_delete_validates_all_jobs_before_removing_any(self):
        one=self.scheduler.save(self.plan())[0];two=self.scheduler.save(self.plan())[0]
        for state in ['preparing','running','queued','receiving','stopping']:
            self.scheduler.jobs[two]['state']=state
            with self.assertRaises(TaskError) as error:self.scheduler.delete_batch({'ids':[one,two]})
            self.assertEqual(error.exception.status,409);self.assertEqual(len(self.scheduler.jobs),2)
        self.scheduler.jobs[two]['state']='waiting';self.scheduler.workers[two]=Mock()
        with self.assertRaises(TaskError):self.scheduler.delete_batch({'ids':[one,two]})
        self.scheduler.workers.clear()
        with self.assertRaises(TaskError) as error:self.scheduler.delete_batch({'ids':[one,'missing']})
        self.assertEqual(error.exception.status,404);self.assertEqual(len(self.scheduler.jobs),2)

    def test_batch_stop_leaves_completed_records_unchanged_and_checks_all_ids(self):
        one=self.scheduler.save(self.plan())[0];done=self.scheduler.save(self.plan())[0]
        self.scheduler.jobs[done].update(state='completed',enabled=False,message='reply kept')
        with self.assertRaises(TaskError):self.scheduler.stop_batch({'ids':[one,'missing']})
        self.assertTrue(self.scheduler.jobs[one]['enabled'])
        self.assertEqual(self.scheduler.stop_batch({'ids':[one,done,one]}),[one])
        self.assertEqual(self.scheduler.jobs[one]['state'],'cancelled')
        self.assertEqual(self.scheduler.jobs[done]['state'],'completed')
        self.assertEqual(self.scheduler.jobs[done]['message'],'reply kept')

    def test_batch_stop_keeps_inflight_replies_and_blocks_delete_until_finished(self):
        started=[];ready=threading.Event();release=threading.Event()
        self.addCleanup(release.set)
        def send(login_id,content,on_sent):
            on_sent();started.append(content)
            if len(started)==2:ready.set()
            release.wait(3);yield {'type':'delta','text':'kept reply'};yield {'type':'done'}
        self.scheduler.send=send
        ids=[self.scheduler.save(self.plan(content=text,repeat_count=3,interval=1))[0] for text in ['one','two']]
        self.scheduler.tick();self.assertTrue(ready.wait(2))
        self.assertEqual(self.scheduler.stop_batch({'ids':ids}),ids)
        self.assertTrue(all(self.scheduler.jobs[key]['state']=='stopping' for key in ids))
        with self.assertRaises(TaskError):self.scheduler.delete_batch({'ids':ids})
        release.set();self.finish()
        self.assertEqual(len(started),2)
        self.assertTrue(all(self.scheduler.jobs[key]['attempts'][0]['text']=='kept reply' for key in ids))
        self.scheduler.delete_batch({'ids':ids});self.assertFalse(self.scheduler.jobs)

    def test_batch_stop_and_delete_reject_invalid_ids_at_entry(self):
        for method in [self.scheduler.stop_batch,self.scheduler.delete_batch]:
            for payload in [None,[],{}, {'ids':[]},{'ids':'one'},{'ids':['']},{'ids':[1]}]:
                with self.subTest(method=method.__name__,payload=payload),self.assertRaises(TaskError):method(payload)

    def test_duplicate_accounts_are_deduplicated_and_invalid_accounts_rejected(self):
        ids=self.scheduler.save(self.plan(login_ids=['a'*32,'a'*32]));self.assertEqual(len(ids),1)
        with self.assertRaises(TaskError):self.scheduler.save(self.plan(login_ids=['missing']))

    def test_twelve_accounts_can_be_saved_together_without_starting_workers(self):
        ids=[format(i,'032x') for i in range(1,13)]
        self.scheduler.account_info=lambda key:{'name':'test','status':'active'} if key in ids else None
        self.scheduler.send=Mock()
        tasks=self.scheduler.save(self.plan(login_ids=ids,repeat_count=3,interval=2))
        self.assertEqual(len(tasks),12)
        self.assertEqual({self.scheduler.jobs[key]['login_id'] for key in tasks},set(ids))
        self.assertTrue(all(self.scheduler.jobs[key]['repeat_count']==3 and self.scheduler.jobs[key]['interval']==2 for key in tasks))
        self.assertFalse(self.scheduler.workers);self.scheduler.send.assert_not_called()

    def test_batch_adjust_changes_only_selected_fields_and_preserves_records(self):
        one=self.scheduler.save(self.plan(content='first',repeat_count=3,interval=2))[0]
        two=self.scheduler.save(self.plan(content='second',repeat_count=4,interval=3))[0]
        third=self.scheduler.save(self.plan(content='untouched'))[0]
        self.scheduler.jobs[one]['attempts']=[{'index':1,'status':'success','text':'previous reply','sent_at':'old'}]
        old={key:dict(job) for key,job in self.scheduler.jobs.items()}
        result=self.scheduler.adjust_batch({'ids':[one,two,one],'changes':{'interval':1}})
        self.assertEqual(result,[one,two]);self.assertEqual(len(self.scheduler.jobs),3)
        for key in [one,two]:
            job=self.scheduler.jobs[key];self.assertEqual(job['interval'],1)
            for field in ['id','login_id','content','repeat_count','run_at','next_run_at','created_at','attempts']:
                self.assertEqual(job[field],old[key][field])
        self.assertEqual(self.scheduler.jobs[third],old[third]);self.assertFalse(self.scheduler.workers)

    def test_batch_rejects_active_task_without_changing_other_tasks(self):
        one=self.scheduler.save(self.plan())[0];two=self.scheduler.save(self.plan())[0]
        self.scheduler.workers[two]=Mock()
        with self.assertRaises(TaskError) as error:
            self.scheduler.adjust_batch({'ids':[one,two],'changes':{'repeat_count':5}})
        self.assertEqual(error.exception.status,409)
        self.assertEqual(self.scheduler.jobs[one]['repeat_count'],1)
        self.scheduler.workers.clear()

    def test_batch_validates_merged_plans_before_any_change(self):
        one=self.scheduler.save(self.plan(interval=1))[0]
        two=self.scheduler.save(self.plan(interval=3600))[0]
        with self.assertRaises(TaskError):
            self.scheduler.adjust_batch({'ids':[one,two],'changes':{'repeat_count':3}})
        self.assertEqual(self.scheduler.jobs[one]['repeat_count'],1)
        for changes in [{},{'login_id':'another'}]:
            with self.assertRaises(TaskError):self.scheduler.adjust_batch({'ids':[one],'changes':changes})

    def test_batch_uses_weekly_next_time_without_overwriting_original_time(self):
        task_id=self.scheduler.save(self.plan(weekly=True))[0]
        original=(datetime.now(SHANGHAI)-timedelta(days=7)).isoformat()
        self.scheduler.jobs[task_id]['run_at']=original
        next_at=self.scheduler.jobs[task_id]['next_run_at']
        self.scheduler.adjust_batch({'ids':[task_id],'changes':{'repeat_count':2}})
        self.assertEqual(self.scheduler.jobs[task_id]['run_at'],original)
        self.assertEqual(self.scheduler.jobs[task_id]['next_run_at'],next_at)


class StorageTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.path=Path(directory.name)/'accounts.sqlite3';codec=Fernet(Fernet.generate_key())
        self.storage=DatabaseLoginStorage(self.path,protect=codec.encrypt,unprotect=codec.decrypt)
        self.info=lambda key:{'name':'test','status':'active'}

    def scheduler(self):return LingxiScheduler(Mock(),Mock(),self.info,storage=self.storage)

    def plan(self):return {'login_ids':['a'*32],'content':'private-custom-content-marker',
        'run_at':(datetime.now(SHANGHAI)+timedelta(days=1)).isoformat()}

    def test_encrypted_tasks_restore_and_do_not_modify_account_table(self):
        scheduler=self.scheduler();task_id=scheduler.save(self.plan())[0]
        recovered=self.scheduler();self.assertEqual(recovered.jobs[task_id]['content'],'private-custom-content-marker')
        self.assertEqual(recovered.jobs[task_id]['repeat_count'],3)
        for path in self.path.parent.glob('accounts.sqlite3*'):
            self.assertNotIn(b'private-custom-content-marker',path.read_bytes())
        self.assertFalse(self.storage.load())

    def test_restart_never_replays_a_request_that_may_have_been_sent(self):
        scheduler=self.scheduler();task_id=scheduler.save(self.plan())[0];job=scheduler.jobs[task_id]
        job['state']='receiving';job['attempts']=[{'index':1,'status':'receiving','sent_at':'previous',
            'planned_at':job['run_at'],'text':'partial','cards':[],'error':''}]
        self.storage.save_lingxi_tasks([job])
        recovered=self.scheduler();recovered.tick()
        self.assertEqual(recovered.jobs[task_id]['state'],'interrupted')
        self.assertFalse(recovered.jobs[task_id]['enabled']);recovered.send.assert_not_called()

    def test_storage_failure_does_not_leave_a_scheduled_job(self):
        scheduler=self.scheduler();self.storage.protect=Mock(side_effect=LoginStorageError('disk failure'))
        with self.assertRaises(TaskError):scheduler.save(self.plan())
        self.assertFalse(scheduler.jobs)

    def test_batch_storage_failure_preserves_database_and_memory(self):
        scheduler=self.scheduler();one=scheduler.save(self.plan())[0];two=scheduler.save(self.plan())[0]
        self.storage.protect=Mock(side_effect=LoginStorageError('disk failure'))
        with self.assertRaises(TaskError):scheduler.adjust_batch({'ids':[one,two],'changes':{'interval':2}})
        self.assertTrue(all(job['interval']==1 for job in scheduler.jobs.values()))
        self.assertTrue(all(job['interval']==1 for job in self.storage.load_lingxi_tasks()))

    def test_batch_delete_rolls_back_database_and_keeps_memory_if_second_delete_fails(self):
        scheduler=self.scheduler();one=scheduler.save(self.plan())[0];two=scheduler.save(self.plan())[0]
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute(f"CREATE TRIGGER fail_delete BEFORE DELETE ON lingxi_tasks WHEN OLD.task_id='{two}' BEGIN SELECT RAISE(ABORT,'unit failure'); END")
        with self.assertRaises(TaskError) as error:scheduler.delete_batch({'ids':[one,two]})
        self.assertEqual(error.exception.status,503)
        self.assertEqual(set(scheduler.jobs),{one,two})
        self.assertEqual({job['id'] for job in self.storage.load_lingxi_tasks()},{one,two})

    def test_batch_delete_is_persisted_without_removing_other_tasks(self):
        scheduler=self.scheduler();one=scheduler.save(self.plan())[0];two=scheduler.save(self.plan())[0]
        scheduler.stop_batch({'ids':[one,two]});scheduler.delete_batch({'ids':[one]})
        recovered=self.scheduler();self.assertEqual(list(recovered.jobs),[two])
        self.assertEqual(recovered.jobs[two]['state'],'cancelled');self.assertFalse(self.storage.load())


class ScheduledWebTests(unittest.TestCase):
    def setUp(self):
        self.app=create_app();self.app.testing=True;self.http=self.app.test_client()
        self.pool=self.app.extensions['unified_logins'];self.scheduler=self.app.extensions['lingxi_scheduler']
        self.saved=self.pool.remember('19900000001','JSESSIONID=unit',{},'old-token','模拟账号')

    def payload(self):return {'login_ids':[self.saved.id],'content':'custom message',
        'run_at':(datetime.now(SHANGHAI)+timedelta(seconds=.15)).isoformat(),'repeat_count':1,'interval':1}

    def test_settings_api_updates_runtime_and_validates_input(self):
        self.assertEqual(self.http.get('/api/lingxi/settings').json['settings']['concurrency'],256)
        response=self.http.post('/api/lingxi/settings',json={'concurrency':512})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.http.get('/api/lingxi/tasks').json['settings']['concurrency'],512)
        self.assertEqual(self.http.post('/api/lingxi/settings',json={'concurrency':False}).status_code,400)

    def test_create_list_edit_stop_delete_and_local_access(self):
        response=self.http.post('/api/lingxi/tasks/save',json=self.payload());self.assertEqual(response.status_code,200)
        task_id=response.json['ids'][0]
        listed=self.http.get('/api/lingxi/tasks').json;self.assertEqual(len(listed['tasks']),1)
        self.assertNotIn(b'old-token',self.http.get('/api/lingxi/tasks').data)
        self.assertEqual(self.http.get('/lingxi/tasks').status_code,200)
        self.assertEqual(self.http.post('/api/lingxi/tasks/save',json=self.payload(),headers={'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.http.post('/api/lingxi/tasks/save',json={**self.payload(),'id':task_id,'content':'changed'}).status_code,200)
        self.assertEqual(self.http.get('/api/lingxi/tasks/'+task_id).json['task']['content'],'changed')
        self.assertEqual(self.http.post('/api/lingxi/tasks/stop',json={'id':task_id}).status_code,200)
        self.assertEqual(self.http.post('/api/lingxi/tasks/delete',json={'id':task_id}).status_code,200)
        self.assertFalse(self.scheduler.jobs)

    def test_logout_stops_future_sends(self):
        task_id=self.http.post('/api/lingxi/tasks/save',json=self.payload()).json['ids'][0]
        self.assertEqual(self.http.post('/api/logins/logout',json={'id':self.saved.id}).status_code,200)
        self.assertFalse(self.scheduler.jobs[task_id]['enabled'])

    def test_batch_api_updates_existing_ids_without_new_jobs(self):
        ids=[self.http.post('/api/lingxi/tasks/save',json=self.payload()).json['ids'][0] for _ in range(2)]
        response=self.http.post('/api/lingxi/tasks/adjust',json={'ids':ids,'changes':{'repeat_count':5,'interval':2}})
        self.assertEqual(response.status_code,200);self.assertEqual(response.json['ids'],ids)
        self.assertEqual(len(self.scheduler.jobs),2)
        self.assertTrue(all(job['repeat_count']==5 and job['interval']==2 for job in self.scheduler.jobs.values()))
        self.assertFalse(self.scheduler.workers)

    def test_batch_stop_and_delete_api_preserve_unselected_records_and_login(self):
        ids=[self.http.post('/api/lingxi/tasks/save',json=self.payload()).json['ids'][0] for _ in range(3)]
        response=self.http.post('/api/lingxi/tasks/stop-batch',json={'ids':ids[:2]})
        self.assertEqual(response.status_code,200);self.assertEqual(response.json['ids'],ids[:2])
        self.assertTrue(self.scheduler.jobs[ids[2]]['enabled'])
        response=self.http.post('/api/lingxi/tasks/delete-batch',json={'ids':ids[:2]})
        self.assertEqual(response.status_code,200);self.assertEqual(response.json['ids'],ids[:2])
        self.assertEqual(list(self.scheduler.jobs),ids[2:]);self.assertIsNotNone(self.pool.get_id(self.saved.id))
        for route in ['stop-batch','delete-batch']:
            self.assertEqual(self.http.post('/api/lingxi/tasks/'+route,json={'ids':[]}).status_code,400)
            self.assertEqual(self.http.post('/api/lingxi/tasks/'+route,json={'ids':ids[2:]},headers={'Origin':'https://evil.example'}).status_code,403)

    @patch('app.verify_token')
    def test_scheduled_send_rechecks_latest_token_immediately_before_post(self,verify):
        seen=[]
        def fake_chat(account,message,*,before_send):
            self.pool.rotate_credentials(self.saved.phone,expected_revision=self.saved.credential_revision,
                cookie='JSESSIONID=new',token='new-token')
            before_send();seen.append((account.auth_code,account.session_id,message))
            yield {'type':'delta','text':'reply'};yield {'type':'done'}
        self.http.post('/api/lingxi/tasks/save',json=self.payload())
        with patch('app.chat',side_effect=fake_chat):
            self.scheduler.tick()
            deadline=time.monotonic()+2
            while self.scheduler.workers and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.scheduler.workers)
        self.assertEqual(seen[0][0],'new-token');self.assertEqual(seen[0][2],'custom message')


if __name__=='__main__':unittest.main()
