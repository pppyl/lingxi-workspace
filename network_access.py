"""Host validation and a shared IPv4/IPv6 listener."""
from ipaddress import ip_address, ip_network
from copy import deepcopy
import os
import re
import socket
from threading import Event, RLock, get_ident
import time
from urllib.parse import urlsplit
from network_interfaces import interface_addresses


def classify_access_host(host, *, allow_domains=True):
    if any(char in host for char in '/?#@\\'):
        return False, False
    try:
        parsed = urlsplit('//'+host)
        hostname, port = parsed.hostname, parsed.port
        if hostname == 'localhost':
            return True, True
    except ValueError:
        return False, False
    try:
        address = ip_address(hostname)
    except ValueError:
        if not hostname or not allow_domains:
            return False, False
        try:
            name = hostname.encode('idna').decode('ascii').rstrip('.')
        except UnicodeError:
            return False, False
        valid = len(name) <= 253 and all(re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', label)
                                         for label in name.split('.'))
        return bool(valid), False
    local_address = (address.ipv4_mapped or address) if address.version == 6 else address
    return not (address.is_unspecified or address.is_multicast or str(address) == '255.255.255.255'), local_address.is_loopback


def allowed_lan_host(host):
    # Standalone legacy apps do not have the workspace password boundary.
    return classify_access_host(host, allow_domains=False)[0]


def is_loopback_peer(peer):
    try:
        address = ip_address(peer)
    except ValueError:
        return False
    return (address.ipv4_mapped or address).is_loopback if address.version == 6 else address.is_loopback


def local_addresses():
    return [ip_address(row['address']) for row in interface_addresses()]


def create_http_server(application, port):
    from waitress import create_server
    manager = getattr(application,'extensions',{}).get('network_settings')
    host = os.environ.get('LINGXI_HOST')
    if host:
        server = create_server(application, host=host, port=port, threads=32, channel_timeout=240)
        if manager:
            manager.attach(server.effective_port,[(server.effective_host,server.effective_port)],override='environment')
        return server
    return ManagedHttpServer(application,port,manager or NetworkSettings())


class ManagedHttpServer:
    """Change accept sockets on the HTTP event loop, retaining in-flight work."""
    def __init__(self, application, port, manager):
        from waitress import create_server
        from waitress.server import BaseWSGIServer
        from waitress.channel import HTTPChannel

        class ListenerChannel(HTTPChannel):
            def readable(self):
                return not getattr(self.server,'retired',False) and super().readable()

            def service(self):
                super().service()
                if getattr(self.server,'retired',False) and not self.requests:
                    self.close_when_flushed = True
                    self.server.pull_trigger()

        self.channel_class = ListenerChannel
        self.application, self.manager = application,manager
        self.changed, self.closed, self.done = Event(),Event(),Event()
        self.lock, self.owner, self.running = RLock(),None,False
        self.draining = []
        try:
            rows = interface_addresses()
        except OSError as error:
            rows = []
            manager.warning = '暂时无法读取网卡地址：'+str(error)
        hosts = manager.plan(rows)
        sockets, effective_port = [],port
        try:
            for host in hosts:
                listener = socket.create_server((host,effective_port),family=socket.AF_INET6 if ':' in host else socket.AF_INET,
                                                dualstack_ipv6=False)
                sockets.append(listener)
                effective_port = listener.getsockname()[1]
            self.backend = create_server(application,sockets=sockets,threads=32,channel_timeout=240)
        except OSError:
            for listener in sockets:listener.close()
            raise
        self.effective_port = effective_port
        self.asyncore, self.adj, self.map = self.backend.asyncore,self.backend.adj,self.backend.map
        self.listeners = {s.effective_host:s for s in self.map.values() if isinstance(s,BaseWSGIServer)}
        for listener in self.listeners.values():listener.channel_class = self.channel_class
        manager.attach(effective_port,[(host,effective_port) for host in self.listeners],server=self)

    def reconcile(self):
        from waitress import create_server
        try:
            rows = interface_addresses()
            with self.manager.lock:
                hosts = self.manager.plan(rows)
                applied = deepcopy(self.manager.settings)
        except OSError as error:
            self.manager.warning = '网卡地址更新失败，将自动重试：'+str(error)
            return
        for host in self.listeners.keys()-set(hosts):
            listener = self.listeners.pop(host)
            # Keep its trigger alive while accepted requests finish. Closing
            # the entire Waitress server would cancel unrelated HTTP work.
            listener.retired = True
            self.asyncore.dispatcher.close(listener)
            for channel in listener.active_channels.values():
                if not channel.requests:channel.close_when_flushed = True
            self.draining.append(listener)
        errors = []
        for host in hosts:
            if host in self.listeners:
                continue
            try:
                sock = socket.create_server((host,self.effective_port),family=socket.AF_INET6 if ':' in host else socket.AF_INET,
                                            dualstack_ipv6=False)
            except OSError as error:
                errors.append(f'{host}：{error}')
                continue
            self.listeners[host] = create_server(self.application,sockets=[sock],map=self.map,
                _dispatcher=self.backend.task_dispatcher,threads=32,channel_timeout=240)
            self.listeners[host].channel_class = self.channel_class
        for listener in self.draining[:]:
            listener.maintenance(time.time())
            if not listener.active_channels:
                listener.trigger.close()
                self.draining.remove(listener)
        with self.manager.lock:
            self.manager.attach(self.effective_port,[(host,self.effective_port) for host in self.listeners],server=self)
            self.manager.warning = '监听更新未完成，将自动重试：'+'；'.join(errors) if errors else ''
            self.manager.applied_settings = None if errors else applied

    def run(self):
        with self.lock:
            self.running,self.owner = True,get_ident()
        next_scan = 0
        try:
            while not self.closed.is_set():
                if self.changed.is_set() or time.monotonic() >= next_scan:
                    self.changed.clear()
                    self.reconcile()
                    next_scan = time.monotonic()+3
                self.asyncore.loop(timeout=.25,count=1,map=self.map,use_poll=self.adj.asyncore_use_poll)
        finally:
            self.backend.close()
            self.done.set()

    def close(self):
        with self.lock:
            self.closed.set()
            if not self.running and not self.done.is_set():
                self.backend.close()
                self.done.set()
        if self.owner != get_ident():self.done.wait(10)


class NetworkError(Exception):
    pass


class NetworkSettings:
    def __init__(self, storage=None):
        self.storage, self.lock = storage,RLock()
        saved = storage.load_network_settings() if storage is not None else None
        self.settings = {'ipv4':True,'ipv6':True,'custom':False,'addresses':[],**(saved or {})}
        self.interfaces = self.settings.pop('interfaces',{})
        self.legacy_addresses = set(self.settings['addresses']) if saved and 'interfaces' not in saved else set()
        self.port, self.bindings, self.active, self.override = None,[],None,''
        self.applied_settings, self.server, self.records = None,None,None
        self.warning = ''

    def attach(self, port, bindings, *, override='', server=None):
        with self.lock:
            self.port, self.bindings, self.override = int(port),list(bindings),override
            addresses = [ip_address(host) for host,_ in bindings]
            self.active = {key:any(address.version == version and not address.is_loopback for address in addresses)
                           for key,version in (('ipv4',4),('ipv6',6))}
            self.applied_settings = deepcopy(self.settings)
            if server is not None:self.server = server

    def plan(self, rows):
        """Follow selected adapter addresses without adding unrelated NICs."""
        with self.lock:
            selected, interfaces = [],{}
            available = {row['address']:row for row in rows}
            reserved = set(self.settings['addresses']) & available.keys()
            for value in self.settings['addresses']:
                original = value
                previous = self.interfaces.get(value)
                row = available.get(value)
                if previous and row and row['interface_id'] != previous['interface_id']:
                    row = None
                if row is None:
                    address = ip_address(value)
                    if previous and previous['interface_id']:
                        candidates = [r for r in rows if r['interface_id'] == previous['interface_id']
                            and r['family'] == previous['family'] and r['address'] not in previous['excluded']
                            and r['address'] not in reserved]
                    elif value in self.legacy_addresses:
                        # Older settings stored only an IP. Migrate only an
                        # unambiguous replacement on the same local prefix.
                        prefix = ip_network(f'{value}/{24 if address.version == 4 else 48}',strict=False)
                        candidates = [r for r in rows if ip_address(r['address']) in prefix and r['address'] not in reserved
                            and (previous is None or r['address'] not in previous['excluded'])]
                        if len(candidates) != 1:candidates = []
                    else:
                        candidates = []
                    row = next(iter(candidates),None)
                if row is not None:
                    value = row['address']
                    self.legacy_addresses.discard(original)
                    reserved.add(value)
                    interfaces[value] = {**previous,'interface_name':row['interface_name']} if previous else self._selection(row,rows,self.settings['addresses'])
                elif previous:
                    interfaces[value] = previous
                selected.append(value)
            settings = {**self.settings,'addresses':sorted(set(selected),key=lambda v:(ip_address(v).version,int(ip_address(v))))}
            if settings != self.settings or interfaces != self.interfaces:
                self._save(settings,interfaces)
            self.records = deepcopy(rows)
            if not settings['custom']:
                return ['::' if settings['ipv6'] else '::1','0.0.0.0' if settings['ipv4'] else '127.0.0.1']
            return ['::1','127.0.0.1',*(value for value in settings['addresses'] if value in available
                and settings[available[value]['family']])]

    @staticmethod
    def _selection(row, rows, selected):
        return {key:row[key] for key in ('interface_id','interface_name','family')} | {
            'excluded':[r['address'] for r in rows if r['interface_id'] == row['interface_id']
                and r['family'] == row['family'] and r['address'] != row['address'] and r['address'] not in selected]}

    def _save(self, settings, interfaces):
        if self.storage is not None:
            self.storage.save_network_settings({**settings,'interfaces':interfaces})
        self.settings, self.interfaces = settings,interfaces

    def update(self, payload):
        if (not isinstance(payload,dict) or set(payload) not in ({'ipv4','ipv6'},{'ipv4','ipv6','custom','addresses'})
                or any(not isinstance(payload[key],bool) for key in ('ipv4','ipv6'))):
            raise NetworkError('请选择是否开启 IPv4、IPv6 对外监听。')
        if 'custom' in payload:
            if not isinstance(payload['custom'],bool) or not isinstance(payload['addresses'],list):
                raise NetworkError('请选择自定义监听地址。')
            selected = set()
            for value in payload['addresses']:
                try:
                    address = ip_address(value) if isinstance(value,str) else None
                except ValueError as error:
                    raise NetworkError('监听地址必须是有效的 IPv4 或 IPv6 地址。') from error
                if (address is None or address.is_loopback or address.is_unspecified or address.is_multicast
                        or address.is_link_local or str(address)=='255.255.255.255'
                        or address.version == 6 and address.ipv4_mapped is not None):
                    raise NetworkError('自定义地址请选择本机的具体网卡 IP；本机回环地址会自动保留。')
                selected.add(address)
            payload = {**payload,'addresses':[str(address) for address in sorted(selected,key=lambda a:(a.version,int(a)))]}
        with self.lock:
            if self.override:
                raise NetworkError('当前监听由环境或手机版控制，无法在此修改。')
            updated = {**self.settings,**payload}
            rows = self.records or []
            interfaces = {}
            for value in updated['addresses']:
                row = next((r for r in rows if r['address'] == value),None)
                if row is not None:interfaces[value] = self._selection(row,rows,updated['addresses'])
                elif value in self.interfaces:interfaces[value] = self.interfaces[value]
            self._save(updated,interfaces)
            if self.server is not None:self.server.changed.set()

    def status(self, fallback_port=5500):
        with self.lock:
            port,active = self.port or fallback_port,self.active
            settings = deepcopy(self.settings)
            bindings = list(self.bindings)
            result = {'settings':settings,'active':dict(active) if active is not None else None,
                'port':port,'managed':not self.override,
                'restart_required':active is not None and not self.override and self.server is None and self.settings != self.applied_settings,
                'applying':self.server is not None and self.settings != self.applied_settings,
                'dynamic':self.server is not None, 'warning':self.warning,
                'bindings':[{'host':host,'port':p} for host,p in self.bindings],
                'local':[f'http://127.0.0.1:{port}/',f'http://[::1]:{port}/'],'ipv4':[],'ipv6':[]}
            if self.override == 'android':
                result['local'] = [f'http://127.0.0.1:{port}/']
        try:
            rows = interface_addresses()
        except OSError:
            rows = []
            result['address_warning'] = '暂时无法读取网卡地址，请刷新后重试。'
        result['addresses'],result['listening'] = [],{'ipv4':[],'ipv6':[]}
        for row in rows:
            address = ip_address(row['address'])
            key = 'ipv4' if address.version == 4 else 'ipv6'
            url = f'http://{str(address) if address.version==4 else "["+str(address)+"]"}:{port}/'
            result[key].append(url)
            listening = (any(host in (str(address),'0.0.0.0' if address.version==4 else '::') for host,_ in bindings)
                         if active is not None else settings[key] and (not settings['custom'] or str(address) in settings['addresses']))
            result['addresses'].append({**row,'url':url,'available':True,'listening':listening})
            if listening:
                result['listening'][key].append(url)
        available = {row['address'] for row in rows}
        result['unavailable'] = [value for value in settings['addresses'] if value not in available]
        for value in result['unavailable']:
            address = ip_address(value)
            result['addresses'].append({'address':value,'url':f'http://{value if address.version==4 else "["+value+"]"}:{port}/',
                'family':'ipv4' if address.version==4 else 'ipv6','available':False,'listening':False,
                'interface_name':self.interfaces.get(value,{}).get('interface_name','')})
        return result


def register_network(app, storage):
    from flask import jsonify,request
    manager = NetworkSettings(storage)
    app.extensions['network_settings'] = manager

    @app.errorhandler(NetworkError)
    def network_error(error):
        return jsonify(error=str(error)),400

    @app.get('/api/network/status')
    def network_status():
        return jsonify(manager.status(int(request.environ.get('SERVER_PORT') or 5500)))

    @app.post('/api/network/settings')
    def network_settings():
        manager.update(request.get_json())
        return jsonify(**manager.status(int(request.environ.get('SERVER_PORT') or 5500)),
                       message='监听设置已保存，正在更新监听，无需重启。')
    return manager
