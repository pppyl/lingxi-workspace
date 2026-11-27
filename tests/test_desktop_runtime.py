from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
from workspace.clients.desktop_browser import DefaultBrowserWindow,wait_for_page
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

    def test_startup_loopback_probe_ignores_system_and_environment_proxies(self):
        local_requests=[];proxy_requests=[]
        class Page(BaseHTTPRequestHandler):
            def do_GET(self):
                local_requests.append(self.path)
                self.send_response(200);self.end_headers();self.wfile.write(b'workbench ready')
            def log_message(self,*args):pass
        class Proxy(BaseHTTPRequestHandler):
            def do_GET(self):
                proxy_requests.append(self.path)
                self.send_response(502);self.end_headers()
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Page)
        proxy=ThreadingHTTPServer(('127.0.0.1',0),Proxy)
        for httpd in (server,proxy):threading.Thread(target=httpd.serve_forever,daemon=True).start()
        proxy_url='http://127.0.0.1:'+str(proxy.server_port)
        environment={key:value for key,value in os.environ.items() if not key.lower().endswith('_proxy')}
        environment.update(http_proxy=proxy_url,https_proxy=proxy_url,no_proxy='')
        try:
            with patch.dict(os.environ,environment,clear=True),patch('urllib.request.getproxies',return_value={'http':proxy_url}):
                wait_for_page('http://127.0.0.1:'+str(server.server_port),timeout=.5)
            self.assertEqual(local_requests,['/'])
            self.assertEqual(proxy_requests,[])
        finally:
            for httpd in (server,proxy):httpd.shutdown();httpd.server_close()

    @unittest.skipUnless(os.name=='nt','Windows desktop controller')
    def test_existing_client_quit_command_uses_direct_loopback_with_proxy_enabled(self):
        seen=[]
        class Control(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path,self.headers.get('Authorization')))
                self.send_response(200);self.end_headers();self.wfile.write(b'{"ok":true}')
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Control)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        environment={key:value for key,value in os.environ.items() if not key.lower().endswith('_proxy')}
        environment.update(http_proxy='http://127.0.0.1:1',https_proxy='http://127.0.0.1:1',no_proxy='')
        try:
            with tempfile.TemporaryDirectory() as folder:
                (Path(folder)/'desktop-instance.json').write_text(json.dumps({'port':server.server_port,'token':'test-control-token'}))
                result=subprocess.run([sys.executable,'desktop.py','--quit','--data-dir',folder],
                    cwd=Path(__file__).resolve().parents[1],env=environment,capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(seen,[('/_desktop/quit','Bearer test-control-token')])
        finally:server.shutdown();server.server_close()

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
            self.assertEqual(Path(result.stdout.strip()),Path(folder)/'LingxiWorkspace/data')

    def test_source_uses_the_same_external_data_directory_as_packaged_client(self):
        with tempfile.TemporaryDirectory() as folder:
            env={key:value for key,value in os.environ.items() if key!='LINGXI_DATA_DIR'}
            env['LOCALAPPDATA']=folder
            result=subprocess.run([sys.executable,'-X','utf8','-c',
                'from runtime_paths import DATA_DIR,RECHARGE_DATA_DIR;print(DATA_DIR);print(RECHARGE_DATA_DIR)'],
                capture_output=True,text=True,env=env,check=True)
            paths=result.stdout.strip().splitlines()
            self.assertEqual(Path(paths[0]),Path(folder)/'LingxiWorkspace/data')
            self.assertEqual(Path(paths[1]),Path(folder)/'LingxiWorkspace/data/recharge')

    def test_project_internal_data_override_is_rejected_before_creating_accounts(self):
        env={**os.environ,'LINGXI_DATA_DIR':str(Path(__file__).resolve().parents[1]/'.local/forbidden-account-storage')}
        result=subprocess.run([sys.executable,'-X','utf8','-c','import runtime_paths'],capture_output=True,text=True,env=env)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('账号数据目录必须位于项目目录外',result.stderr)
