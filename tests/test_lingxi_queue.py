from datetime import datetime,timedelta
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from cryptography.fernet import Fernet

from lingxi_scheduler import LingxiScheduler,TaskError,SHANGHAI
from login_storage import DatabaseLoginStorage,LoginStorageError


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.scheduler=LingxiScheduler(Mock(),self.send,lambda key:{'name':'test','status':'active'})
        self.addCleanup(self.scheduler.shutdown)

    def send(self,login_id,content,on_sent):
        on_sent();yield {'type':'delta','text':'reply'};yield {'type':'done'}

    def schedule(self,count=3):
        task_id=self.scheduler.save({'login_ids':['a'*32],'content':'unit-test only',
            'run_at':(datetime.now(SHANGHAI)+timedelta(seconds=.15)).isoformat(),
            'repeat_count':count,'interval':.2})[0]
        self.scheduler.tick();return task_id

    def wait(self,predicate,timeout=4):
        with self.scheduler.condition:self.assertTrue(self.scheduler.condition.wait_for(predicate,timeout))

    def finish(self):
        deadline=time.monotonic()+5
        while self.scheduler.workers and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.scheduler.workers)

    def test_default_and_setting_validation(self):
        self.assertEqual(self.scheduler.settings(),{'concurrency':256,'running':0,'queued':0})
        for value in [0,-1,1025,True,1.5,'256',None]:
            with self.subTest(value=value),self.assertRaises(TaskError):
                self.scheduler.update_settings({'concurrency':value})
        self.assertEqual(self.scheduler.settings()['concurrency'],256)

    def test_overflow_waits_fifo_past_two_seconds_and_every_request_is_sent_once(self):
        release=threading.Event();self.addCleanup(release.set);calls=[]
        def send(login_id,content,on_sent):
            on_sent();calls.append(time.monotonic())
            if len(calls)==1:release.wait(5)
            yield {'type':'delta','text':'reply'};yield {'type':'done'}
        self.scheduler.send=send;self.scheduler.update_settings({'concurrency':1})
        task_id=self.schedule();self.wait(lambda:len(self.scheduler.pending)==2)
        self.assertEqual(self.scheduler.settings()['running'],1)
        # Queue waiting is deliberately longer than the old two-second late cutoff.
        self.assertFalse(release.wait(2.1));release.set();self.finish()
        task=self.scheduler.snapshots(task_id)[0]
        self.assertEqual(task['state'],'completed');self.assertEqual(task['sent_count'],3)
        self.assertEqual(len(calls),3)
        self.assertEqual([a['index'] for a in sorted(task['attempts'],key=lambda a:a['sent_at'])],[1,2,3])
        self.assertGreater(task['attempts'][2]['queue_wait_seconds'],2)
        self.assertEqual(self.scheduler.settings()['queued'],0)

    def test_stopping_removes_waiting_requests_and_keeps_already_sent_reply(self):
        release=threading.Event();self.addCleanup(release.set);calls=[]
        def send(login_id,content,on_sent):
            on_sent();calls.append(content);release.wait(4)
            yield {'type':'delta','text':'kept reply'};yield {'type':'done'}
        self.scheduler.send=send;self.scheduler.update_settings({'concurrency':1})
        task_id=self.schedule();self.wait(lambda:len(self.scheduler.pending)==2)
        self.scheduler.stop(task_id)
        self.assertEqual(self.scheduler.settings()['queued'],0)
        release.set();self.finish();task=self.scheduler.snapshots(task_id)[0]
        self.assertEqual(len(calls),1);self.assertEqual(task['state'],'cancelled')
        self.assertEqual(task['attempts'][0]['text'],'kept reply')
        self.assertTrue(all(a['status']=='cancelled' for a in task['attempts'][1:]))

    def test_increasing_dispatches_immediately_and_lowering_does_not_interrupt_running(self):
        first=threading.Event();second=threading.Event();second_started=threading.Event()
        self.addCleanup(first.set);self.addCleanup(second.set)
        calls=[]
        def send(login_id,content,on_sent):
            on_sent();calls.append(content);index=len(calls)
            if index==1:first.wait(4)
            elif index==2:second_started.set();second.wait(4)
            yield {'type':'done'}
        self.scheduler.send=send;self.scheduler.update_settings({'concurrency':1})
        self.schedule();self.wait(lambda:len(self.scheduler.pending)==2)
        self.scheduler.update_settings({'concurrency':2})
        self.assertTrue(second_started.wait(1));self.assertEqual(self.scheduler.settings()['running'],2)
        self.scheduler.update_settings({'concurrency':1});first.set()
        self.wait(lambda:self.scheduler.running_requests==1)
        self.assertEqual(self.scheduler.settings()['queued'],1)
        self.assertEqual(len(calls),2)
        second.set();self.finish();self.assertEqual(len(calls),3)

    def test_shutdown_cancels_queue_without_dispatching_it(self):
        release=threading.Event();self.addCleanup(release.set);calls=[]
        def send(login_id,content,on_sent):
            on_sent();calls.append(content);release.wait(4);yield {'type':'done'}
        self.scheduler.send=send;self.scheduler.update_settings({'concurrency':1})
        task_id=self.schedule();self.wait(lambda:len(self.scheduler.pending)==2)
        self.scheduler.shutdown();self.wait(lambda:len(self.scheduler.pending)==0)
        release.set();self.finish()
        self.assertEqual(len(calls),1);self.assertEqual(self.scheduler.jobs[task_id]['state'],'interrupted')

    def test_latest_reply_uses_successful_completion_and_ignores_errors_and_progress(self):
        task_id=self.scheduler.save({'login_ids':['a'*32],'content':'test','run_at':
            (datetime.now(SHANGHAI)+timedelta(days=1)).isoformat()})[0]
        self.scheduler.jobs[task_id]['attempts']=[
            {'index':1,'status':'success','text':'latest valid','finished_at':'2026-10-29T10:00:10+08:00'},
            {'index':2,'status':'success','text':'older completion','finished_at':'2026-10-29T10:00:05+08:00'},
            {'index':3,'status':'failed','text':'incomplete answer','finished_at':'2026-10-29T10:00:15+08:00'},
            {'index':4,'status':'receiving','text':'still streaming','finished_at':'2026-10-29T10:00:20+08:00'},
            {'index':5,'status':'success','text':'','cards':[],'finished_at':'2026-10-29T10:00:25+08:00'},
        ]
        reply=self.scheduler.snapshots()[0]['last_reply']
        self.assertEqual(reply['text'],'latest valid');self.assertEqual(reply['index'],1)

    def test_previous_round_valid_reply_remains_visible_until_new_answer(self):
        task_id=self.scheduler.save({'login_ids':['a'*32],'content':'test','run_at':
            (datetime.now(SHANGHAI)+timedelta(days=1)).isoformat()})[0]
        self.scheduler.jobs[task_id]['history']=[{'attempts':[{'index':2,'status':'success','text':'last week reply',
            'finished_at':'2026-10-28T10:00:00+08:00'}]}]
        self.assertEqual(self.scheduler.snapshots()[0]['last_reply']['text'],'last week reply')


class QueueStorageTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        codec=Fernet(Fernet.generate_key())
        self.storage=DatabaseLoginStorage(Path(directory.name)/'test.sqlite3',protect=codec.encrypt,unprotect=codec.decrypt)

    def scheduler(self):return LingxiScheduler(Mock(),Mock(),lambda key:{'status':'active'},storage=self.storage)

    def test_setting_is_persisted_and_restored(self):
        scheduler=self.scheduler();scheduler.update_settings({'concurrency':512})
        self.assertEqual(self.scheduler().settings()['concurrency'],512)
        self.assertEqual(self.storage.load_lingxi_tasks(),[])

    def test_failed_save_keeps_previous_runtime_limit(self):
        scheduler=self.scheduler()
        self.storage.save_lingxi_concurrency=Mock(side_effect=LoginStorageError('disk failure'))
        with self.assertRaises(TaskError):scheduler.update_settings({'concurrency':512})
        self.assertEqual(scheduler.settings()['concurrency'],256)

    def test_restart_does_not_replay_queued_requests(self):
        scheduler=self.scheduler();task_id=scheduler.save({'login_ids':['a'*32],'content':'test',
            'run_at':(datetime.now(SHANGHAI)+timedelta(days=1)).isoformat()})[0]
        job=scheduler.jobs[task_id];job['state']='queued'
        job['attempts']=[{'index':1,'status':'queued','sent_at':None,'text':'','cards':[]}]
        self.storage.save_lingxi_tasks([job]);restored=self.scheduler()
        self.assertEqual(restored.jobs[task_id]['state'],'interrupted')
        self.assertEqual(restored.jobs[task_id]['attempts'][0]['status'],'interrupted')
        self.assertFalse(restored.jobs[task_id]['enabled']);restored.send.assert_not_called()


if __name__=='__main__':unittest.main()
