"""Host validation and a shared IPv4/IPv6 listener."""
from copy import deepcopy
import errno
from ipaddress import ip_address, ip_network
import logging
import os
import re
import socket
from threading import Event, Lock, RLock, get_ident
import time
from urllib.parse import urlsplit
from workspace.network_interfaces import interface_addresses


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


def _listen(host, port):
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    if os.name != 'nt':
        return socket.create_server((host, port), family=family, dualstack_ipv6=False)
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        # Winsock otherwise allows a wildcard listener to overlap an occupied
        # loopback address, leaving desktop requests on the original listener.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        if family == socket.AF_INET6:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        listener.bind((host, port))
        listener.listen()
    except OSError:
        listener.close()
        raise
    return listener


def create_http_server(application, port=None, *, fallback_if_occupied=False, on_port_changed=None):
    from waitress import create_server
    manager = getattr(application,'extensions',{}).get('network_settings') or NetworkSettings()
    host = os.environ.get('LINGXI_HOST')
    override = os.environ.get('LINGXI_PORT') if port is None else None
    port_editable = override is None and port in (None,0)
    if port is None:
        port = int(override) if override is not None else manager.settings.get('port', 5500)
    if host:
        server = create_server(application, host=host, port=port, threads=32, channel_timeout=240)
        manager.attach(server.effective_port,[(server.effective_host,server.effective_port)],override='environment')
        return server
    return ManagedHttpServer(application,port,manager,port_editable=port_editable,
                             fallback_if_occupied=fallback_if_occupied,on_port_changed=on_port_changed)


class ManagedHttpServer:
    """Change accept sockets on the HTTP event loop, retaining in-flight work."""
    def __init__(self, application, port, manager, *, port_editable, fallback_if_occupied, on_port_changed):
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
        self.port_editable, self.on_port_changed = port_editable,on_port_changed
        self.changed, self.closed, self.done = Event(),Event(),Event()
        self.lock, self.owner, self.running = RLock(),None,False
        self.settings_change = None
        self.draining = []
        try:
            rows = interface_addresses()
        except OSError as error:
            rows = []
            manager.warning = '暂时无法读取网卡地址：'+str(error)
        hosts = manager.plan(rows)
        try:
            sockets = self._bind(hosts,port)
        except OSError as error:
            if not fallback_if_occupied or not port or error.errno not in (errno.EADDRINUSE,10048,10013):raise
            sockets = self._bind(hosts,0)
            manager.warning = f'端口 {port} 无法监听，已使用空闲端口。'
        self.backend = create_server(application,sockets=sockets,threads=32,channel_timeout=240)
        self.effective_port = sockets[0].getsockname()[1]
        self.asyncore, self.adj, self.map = self.backend.asyncore,self.backend.adj,self.backend.map
        self.listeners = {s.effective_host:s for s in self.map.values() if isinstance(s,BaseWSGIServer)}
        for listener in self.listeners.values():listener.channel_class = self.channel_class
        manager.attach(self.effective_port,[(host,self.effective_port) for host in self.listeners],server=self)

    @staticmethod
    def _bind(hosts, port):
        sockets = []
        try:
            for host in hosts:
                listener = _listen(host, port)
                sockets.append(listener)
                port = listener.getsockname()[1]
        except OSError:
            for listener in sockets:listener.close()
            raise
        return sockets

    def _retire(self, listener):
        # Keep its trigger alive while accepted requests finish.
        listener.retired = True
        self.asyncore.dispatcher.close(listener)
        for channel in listener.active_channels.values():
            if not channel.requests:channel.close_when_flushed = True
        self.draining.append(listener)

    def change_port(self, settings, interfaces):
        done = Event()
        with self.lock:
            if self.closed.is_set():
                raise NetworkError('本地监听服务未运行，端口未修改。')
            if not self.running:
                self._apply_port(settings,interfaces)
                return
            change = {'settings':settings,'interfaces':interfaces,'done':done}
            self.settings_change = change
            self.changed.set()
        done.wait()
        if 'error' in change:raise change['error']

    def _apply_port(self, settings, interfaces):
        from waitress import create_server
        from waitress.server import BaseWSGIServer

        port = settings['port']
        manager = self.manager
        with manager.lock:
            hosts = manager._hosts(settings, {row['address']:row for row in manager.records or []})
        try:
            sockets = self._bind(hosts,port)
        except OSError as error:
            raise NetworkError(f'无法监听端口 {port}，请检查是否被占用或系统是否允许绑定。') from error
        # Stage the replacement on the event loop before accepting any requests there.
        existing = set(self.map)
        try:
            create_server(self.application,sockets=sockets,map=self.map,
                          _dispatcher=self.backend.task_dispatcher,threads=32,channel_timeout=240)
            with manager.lock:
                manager._save(settings,interfaces)
        except Exception:
            for key in self.map.keys()-existing:self.map[key].close()
            for listener in sockets:listener.close()
            raise
        new_listeners = [self.map[key] for key in self.map.keys()-existing
                         if isinstance(self.map[key],BaseWSGIServer)]
        for listener in self.listeners.values():self._retire(listener)
        self.listeners = {listener.effective_host:listener for listener in new_listeners}
        for listener in self.listeners.values():listener.channel_class = self.channel_class
        self.effective_port = port
        manager.attach(port,[(host,port) for host in self.listeners],server=self)
        if self.on_port_changed:
            try:self.on_port_changed(port)
            except OSError:
                logging.getLogger(__name__).exception('Could not update the desktop access address')

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
            self._retire(self.listeners.pop(host))
        errors = []
        for host in hosts:
            if host in self.listeners:
                continue
            try:
                sock = _listen(host, self.effective_port)
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
                with self.lock:
                    change,self.settings_change = self.settings_change,None
                if change:
                    try:
                        self._apply_port(change['settings'],change['interfaces'])
                    except Exception as error:
                        change['error'] = error
                    finally:
                        change['done'].set()
                if self.changed.is_set() or time.monotonic() >= next_scan:
                    self.changed.clear()
                    self.reconcile()
                    next_scan = time.monotonic()+3
                self.asyncore.loop(timeout=.25,count=1,map=self.map,use_poll=self.adj.asyncore_use_poll)
        finally:
            with self.lock:
                self.closed.set()
                if self.settings_change:
                    self.settings_change['error'] = NetworkError('本地监听服务已停止，端口未修改。')
                    self.settings_change['done'].set()
                    self.settings_change = None
            self._close_backend()

    def _close_backend(self):
        try:
            self.backend.task_dispatcher.shutdown()
            self.asyncore.close_all(self.map)
        finally:self.done.set()

    def close(self):
        with self.lock:
            self.closed.set()
            if not self.running and not self.done.is_set():
                self._close_backend()
        if self.owner != get_ident():self.done.wait(10)


class NetworkError(Exception):
    pass


class NetworkSettings:
    def __init__(self, storage=None):
        self.storage, self.lock = storage,RLock()
        self.update_lock = Lock()
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
            return self._hosts(settings,available)

    @staticmethod
    def _hosts(settings, available):
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
        if (not isinstance(payload,dict) or set(payload)-{'port'} not in ({'ipv4','ipv6'},{'ipv4','ipv6','custom','addresses'})
                or any(not isinstance(payload[key],bool) for key in ('ipv4','ipv6'))):
            raise NetworkError('请选择是否开启 IPv4、IPv6 对外监听。')
        if 'port' in payload and (type(payload['port']) is not int or not 1 <= payload['port'] <= 65535):
            raise NetworkError('端口请输入 1 至 65535 的整数。')
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
        with self.update_lock:
            with self.lock:
                if self.override:
                    raise NetworkError('当前监听由环境或手机版控制，无法在此修改。')
                server = self.server
                if 'port' in payload and (server is None or not server.port_editable):
                    raise NetworkError('当前端口由运行环境控制，无法在此修改。')
                updated = {**self.settings,**payload}
                rows = self.records or []
                interfaces = {}
                for value in updated['addresses']:
                    row = next((r for r in rows if r['address'] == value),None)
                    if row is not None:interfaces[value] = self._selection(row,rows,updated['addresses'])
                    elif value in self.interfaces:interfaces[value] = self.interfaces[value]
                if 'port' not in payload or payload['port'] == self.port:
                    self._save(updated,interfaces)
                    if server is not None:server.changed.set()
                    return False
            server.change_port(updated,interfaces)
            return True

    def sidebar(self):
        with self.lock:
            if self.records is None and (self.active is None or any(self.active.values())):
                return None
            state = self.status(last_scan=True)
        return {'active': state['active'] or state['settings'], 'listening': state['listening']}

    def status(self, fallback_port=5500, *, last_scan=False):
        with self.lock:
            port,active = self.port or fallback_port,self.active
            settings = deepcopy(self.settings)
            bindings = list(self.bindings)
            rows = self.records or []
            result = {'settings':settings,'active':dict(active) if active is not None else None,
                'port':port,'managed':not self.override,
                'port_editable':self.server is not None and self.server.port_editable,
                'restart_required':active is not None and not self.override and self.server is None and self.settings != self.applied_settings,
                'applying':self.server is not None and self.settings != self.applied_settings,
                'dynamic':self.server is not None, 'warning':self.warning,
                'bindings':[{'host':host,'port':p} for host,p in self.bindings],
                'local':[f'http://127.0.0.1:{port}/',f'http://[::1]:{port}/'],'ipv4':[],'ipv6':[]}
            if self.override == 'android':
                result['local'] = [f'http://127.0.0.1:{port}/']
        if not last_scan:
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
    app.add_template_global(manager.sidebar, name='workspace_network')

    @app.errorhandler(NetworkError)
    def network_error(error):
        return jsonify(error=str(error)),400

    @app.get('/api/network/status')
    def network_status():
        return jsonify(manager.status(int(request.environ.get('SERVER_PORT') or 5500)))

    @app.post('/api/network/settings')
    def network_settings():
        port_changed = manager.update(request.get_json())
        return jsonify(**manager.status(int(request.environ.get('SERVER_PORT') or 5500),last_scan=port_changed),
                       port_changed=port_changed,
                       message=('监听已重启，正在打开新端口。' if port_changed else '监听设置已保存，正在更新监听，无需重启。'))
    return manager
