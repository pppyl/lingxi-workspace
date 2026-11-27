"""Account-bound ConstID registration, using the shared Python C1 protocol."""
import hashlib
import os
import platform
import time
from threading import RLock
from datetime import datetime

from recharge.device import DeviceSession, DeviceError, APP_ID, SERVER
from recharge.cmcc import DEVICE_STATE_SCOPE
from runtime_paths import VERSION
from workspace.http_transport import requests
from features.checkin.client import CheckinError

SCOPE = 'native-group-device:' + APP_ID


class GroupDevices:
    def __init__(self, cache, shared_storage=None):
        self.cache, self.shared_storage = cache, shared_storage
        self.lock = RLock()
        self.devices = {}

    @staticmethod
    def descriptor(user_agent):
        result = {'pro': -1, 'ua': user_agent, 'np': platform.system(),
                  'to': -int(datetime.now().astimezone().utcoffset().total_seconds() / 60)}
        if os.cpu_count() is not None:
            result['hc'] = os.cpu_count()
        return result

    def ensure(self, snapshot):
        key = self.cache.account(snapshot)
        account_key = hashlib.sha256((snapshot['phone'] + '|' + APP_ID).encode()).hexdigest()[:24]
        user_agent = f'LingxiWorkspace/{VERSION} (Python/{platform.python_version()}; {platform.system()} {platform.release()})'
        with self.lock:
            existing = self.devices.get(key)
            if existing is None:
                saved = self.cache.peek(SCOPE, snapshot)
                state = saved['data']['state'] if saved else None
                if not state and self.shared_storage:
                    prior = (self.shared_storage.load_activity_data(DEVICE_STATE_SCOPE) or {}).get(account_key)
                    if prior and prior.get('sdk_version') == '1.1511.0' and isinstance(prior.get('cache'), str):
                        state = {'account_key': account_key, 'app_id': APP_ID, 'server': SERVER,
                                 'lid': prior['cache'], 'const_id': '', 'issued_at': None, 'expires_at': None}
                http = requests.Session(headers={'User-Agent': user_agent})
                device = DeviceSession(account_key, http=http, state=state, user_agent=user_agent,
                                       fingerprint_provider=lambda: self.descriptor(user_agent))
                existing = {'device': device, 'http': http,
                            'generation': saved['generation'] if saved else None, 'stored': state}
                self.devices[key] = existing
        try:
            return existing['device'].ensure()
        except DeviceError as error:
            raise CheckinError(str(error), error.code, 409) from error
        except requests.RequestsError as error:
            raise CheckinError('官方抽奖认证连接失败，请稍后主动重试。', 'DEVICE_NETWORK', 502) from error
        finally:
            with self.lock:
                if self.devices.get(key) is existing:
                    state = existing['device'].export_state()
                    if state != existing['stored']:
                        saved = self.cache.install(SCOPE, snapshot, existing['http'], {'state': state},
                            expiry_cookies=(), expected_generation=existing['generation'], replace_identity=False)
                        if saved:
                            existing.update(generation=saved['generation'], stored=state)

    def ready(self, snapshot):
        saved = self.cache.load(SCOPE, snapshot)
        if not saved:
            return False
        state = saved['data']['state']
        return bool(state.get('const_id') and (state.get('expires_at') is None or state['expires_at'] > time.time()))

    def forget(self, login_id):
        with self.lock:
            item = self.devices.pop(login_id, None)
            if item:
                item['http'].close()
            self.cache.forget(login_id, scope=SCOPE)

    def close(self):
        with self.lock:
            for item in self.devices.values():
                item['http'].close()
            self.devices.clear()
