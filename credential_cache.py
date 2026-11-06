"""Account/realm scoped reusable credentials, not cached business responses."""
import base64
from copy import deepcopy
from http.cookiejar import Cookie
import json
import re
import secrets
from threading import RLock
import time


def token_expiry(token):
    """An HTTPS-issued JWT's exp is an expiry hint, not signature validation."""
    parts = token.split('.') if isinstance(token,str) else []
    if len(parts) != 3:return None
    try:
        value = json.loads(base64.urlsafe_b64decode(parts[1]+'='*(-len(parts[1])%4)))
    except (ValueError,UnicodeError):
        return None
    expiry = value.get('exp') if isinstance(value,dict) else None
    return expiry if isinstance(expiry,(int,float)) and not isinstance(expiry,bool) else None


class CredentialCache:
    def __init__(self, storage=None, *, now=time.time):
        self.storage, self.now = storage, now
        self.entries, self.locks, self.lock = {}, {}, RLock()
        self.failures = {}

    @staticmethod
    def account(snapshot):
        return snapshot.get('id') or snapshot['phone']

    def _key_lock(self, scope, login_id):
        with self.lock:
            return self.locks.setdefault((scope,login_id),RLock())

    def load(self, scope, snapshot):
        key = scope,self.account(snapshot)
        with self.lock:
            if key not in self.entries:
                self.entries[key] = self.storage.load_credentials(*key) if self.storage else None
            saved = self.entries[key]
            if saved and saved['phone'] == snapshot['phone'] and (saved['expires_at'] is None or saved['expires_at'] > self.now()):
                return deepcopy(saved)
        return None

    @staticmethod
    def _expiry(cookies, headers, names=None):
        relevant=[cookie for cookie in cookies if names is None or cookie['name'] in names]
        expiries=[token_expiry(cookie['value']) for cookie in relevant]
        expiries += [cookie['expires'] for cookie in relevant if names is not None or re.search('session|token|ticket',cookie['name'],re.I)]
        expiries += [token_expiry(value.removeprefix('Bearer ')) for value in headers.values()]
        return min((value for value in expiries if value is not None),default=None)

    def connect(self, scope, snapshot, client, initialize, *, headers=(), aliases=(), expiry_cookies=None):
        """Coalesce bootstrap only; each caller keeps its own operation session."""
        key = scope,self.account(snapshot)
        requested_at = time.monotonic()
        with self._key_lock(*key):
            if expiry_cookies is not None:
                names=list(expiry_cookies)
                with self.lock:
                    if key not in self.entries:self.entries[key]=self.storage.load_credentials(*key) if self.storage else None
                    previous=self.entries[key]
                    if previous and previous['phone']==snapshot['phone'] and previous.get('expiry_cookies')!=names:
                        previous={**previous,'expiry_cookies':names,'expires_at':self._expiry(previous['cookies'],previous['headers'],names)}
                        if self.storage:self.storage.save_credentials(*key,previous)
                        self.entries[key]=previous
            saved = self.load(scope,snapshot)
            if not saved and aliases:
                compatible = [value for alias in aliases if alias != scope
                              if (value := self.load(alias, snapshot))]
                if compatible:
                    saved = max(compatible, key=lambda value: value['created_at'])
                    if self.storage:self.storage.save_credentials(*key,saved)
                    with self.lock:self.entries[key] = deepcopy(saved)
            if saved:
                client.headers.update(saved['headers'])
                for value in saved['cookies']:
                    if value['expires'] is not None and value['expires'] <= self.now():continue
                    client.cookies.jar.set_cookie(Cookie(0,value['name'],value['value'],None,False,
                        value['domain'],value.get('domain_specified',bool(value['domain'])),
                        value.get('domain_initial_dot',value['domain'].startswith('.')),
                        value['path'],value.get('path_specified',True),
                        value['secure'],value['expires'],value['expires'] is None,None,None,value.get('rest',{})))
            else:
                failed = self.failures.get(key)
                if failed and failed[0] >= requested_at:
                    raise failed[1]
                try:
                    data = initialize()
                except Exception as error:
                    self.failures[key] = time.monotonic(),error
                    raise
                self.failures.pop(key,None)
                cookies = [{'name':c.name,'value':c.value,'domain':c.domain,'path':c.path,'secure':c.secure,
                            'domain_specified':c.domain_specified,'domain_initial_dot':c.domain_initial_dot,
                            'path_specified':c.path_specified,'expires':c.expires,'rest':c._rest} for c in client.cookies.jar]
                selected = {k:client.headers[k] for k in headers if k in client.headers}
                saved = {'phone':snapshot['phone'],'data':data,'cookies':cookies,'headers':selected,
                    'expires_at':self._expiry(cookies,selected,expiry_cookies),
                    'generation':secrets.token_hex(12),'created_at':self.now()}
                if expiry_cookies is not None:saved['expiry_cookies']=list(expiry_cookies)
                # Do not invent a lifetime for opaque tokens or session cookies.
                if self.storage:self.storage.save_credentials(*key,saved)
                with self.lock:self.entries[key] = saved
            client._credential_binding = (scope,snapshot,initialize,tuple(headers),saved['generation'])
            client._credential_expiry_cookies=saved.get('expiry_cookies')
            client._credential_data = deepcopy(saved['data'])
            return deepcopy(saved['data'])

    def invalidate(self, client):
        scope,snapshot,_,_,generation = client._credential_binding
        key = scope,self.account(snapshot)
        with self._key_lock(*key), self.lock:
            current = self.entries.get(key)
            if current and current['generation'] == generation:
                if self.storage:self.storage.delete_credentials(login_id=key[1],scope=scope)
                self.entries.pop(key,None)

    def sync(self, client):
        """Persist server-rotated cookies for the same established auth generation."""
        scope,snapshot,_,headers,generation = client._credential_binding
        key = scope,self.account(snapshot)
        with self._key_lock(*key), self.lock:
            saved = self.entries.get(key)
            if saved is None or saved['generation'] != generation:
                return
            cookies = [{'name':c.name,'value':c.value,'domain':c.domain,'path':c.path,'secure':c.secure,
                        'domain_specified':c.domain_specified,'domain_initial_dot':c.domain_initial_dot,
                        'path_specified':c.path_specified,'expires':c.expires,'rest':c._rest} for c in client.cookies.jar]
            selected = {name:client.headers[name] for name in headers if name in client.headers}
            if cookies == saved['cookies'] and selected == saved['headers']:
                return
            updated = {**saved,'cookies':cookies,'headers':selected,'expires_at':self._expiry(cookies,selected,saved.get('expiry_cookies'))}
            if self.storage:self.storage.save_credentials(*key,updated)
            self.entries[key] = updated

    def recover(self, client):
        """One recovery after an explicit auth rejection of a read-only request."""
        if getattr(client,'_credential_recovered',False):return False
        client._credential_recovered = True
        scope,snapshot,initialize,headers,_ = client._credential_binding
        self.invalidate(client)
        client.cookies.clear()
        for header in headers:client.headers.pop(header,None)
        self.connect(scope,snapshot,client,initialize,headers=headers,expiry_cookies=getattr(client,'_credential_expiry_cookies',None))
        return True

    def forget(self, login_id=None, *, scope=None):
        with self.lock:
            if self.storage:self.storage.delete_credentials(login_id=login_id,scope=scope)
            self.entries = {key:value for key,value in self.entries.items()
                            if not ((login_id is None or key[1] == login_id) and (scope is None or key[0] == scope))}
            self.failures = {key:value for key,value in self.failures.items()
                            if not ((login_id is None or key[1] == login_id) and (scope is None or key[0] == scope))}
