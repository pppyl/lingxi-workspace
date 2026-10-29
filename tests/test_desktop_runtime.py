from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock
from desktop_browser import DefaultBrowserWindow,wait_for_page
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer


class DesktopRuntimeTests(unittest.TestCase):
    def test_repeated_launch_reuses_browser_before_and_after_page_connects(self):
        opener=Mock(return_value=True);focus=Mock()
        window=DefaultBrowserWindow('http://127.0.0.1:5500/?desktop=1',opener=opener,focus=focus)
        self.assertTrue(window.open());self.assertFalse(window.open())
        window.attach('a'*32)
        self.assertFalse(window.open());self.assertFalse(window.open())
        opener.assert_called_once();self.assertEqual(focus.call_count,3)
        self.assertEqual(window.opens,1)
        window.detach('a'*32);self.assertTrue(window.open());self.assertEqual(opener.call_count,2)

    def test_concurrent_open_commands_only_launch_once(self):
        opener=Mock(return_value=True)
        window=DefaultBrowserWindow('http://127.0.0.1:5500/',opener=opener,focus=Mock())
        workers=[threading.Thread(target=window.open) for _ in range(8)]
        for worker in workers:worker.start()
        for worker in workers:worker.join()
        opener.assert_called_once()

    def test_startup_waits_for_first_successful_page_response(self):
        ready=threading.Event();seen=[]
        class Page(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(ready.is_set())
                self.send_response(200 if ready.is_set() else 503);self.end_headers();self.wfile.write(b'page')
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Page)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        timer=threading.Timer(.2,ready.set);timer.start()
        try:
            wait_for_page('http://127.0.0.1:'+str(server.server_port),timeout=2)
            self.assertIn(False,seen);self.assertTrue(seen[-1])
        finally:server.shutdown();server.server_close();timer.join()

    def test_desktop_data_override_does_not_use_developer_accounts(self):
        with tempfile.TemporaryDirectory() as folder:
            code='''
import json
from pathlib import Path
from app import app
from runtime_paths import DATA_DIR,RECHARGE_DATA_DIR
assert not app.extensions['unified_logins'].public()
assert not app.extensions['lingxi_scheduler'].jobs
activity=app.extensions['recharge_app']
assert not activity.extensions['scheduler'].jobs
assert activity.extensions['scheduler'].storage==RECHARGE_DATA_DIR/'schedules.json'
assert activity.extensions['accounts'].get().client.bridge.profile_dir.is_relative_to(DATA_DIR)
print(json.dumps({'database':str(DATA_DIR/'accounts.sqlite3'),'activity':str(RECHARGE_DATA_DIR)}))
'''
            result=subprocess.run([sys.executable,'-X','utf8','-c',code],capture_output=True,text=True,
                env={**os.environ,'LINGXI_DATA_DIR':folder},check=True)
            data=json.loads(result.stdout)
            self.assertEqual(Path(data['database']),Path(folder)/'accounts.sqlite3')
            self.assertEqual(Path(data['activity']),Path(folder)/'recharge')

    def test_frozen_defaults_use_local_appdata(self):
        with tempfile.TemporaryDirectory() as folder:
            env={key:value for key,value in os.environ.items() if key!='LINGXI_DATA_DIR'}
            env['LOCALAPPDATA']=folder
            result=subprocess.run([sys.executable,'-X','utf8','-c',
                'import sys;sys.frozen=True;from runtime_paths import DATA_DIR;print(DATA_DIR)'],
                capture_output=True,text=True,env=env,check=True)
            self.assertEqual(Path(result.stdout.strip()),Path(folder)/'LingxiWorkspace')
