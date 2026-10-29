from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class DesktopRuntimeTests(unittest.TestCase):
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
