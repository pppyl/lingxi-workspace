"""Host validation and a shared IPv4/IPv6 listener."""
from ipaddress import ip_address
import os
import re
import socket
from threading import RLock
from urllib.parse import urlsplit


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


def create_http_server(application, port):
    from waitress import create_server
    manager = getattr(application,'extensions',{}).get('network_settings')
    host = os.environ.get('LINGXI_HOST')
    if host:
        server = create_server(application, host=host, port=port, threads=32, channel_timeout=240)
        if manager:
            manager.attach(server.effective_port,[(host,server.effective_port)],override='environment')
        return server
    settings = manager.settings if manager else {'ipv4':True,'ipv6':True}
    # Separate sockets let each family bind to all interfaces or loopback only.
    listeners = []
    try:
        listener6 = socket.create_server(('::' if settings['ipv6'] else '::1',port),
                                         family=socket.AF_INET6,dualstack_ipv6=False)
        listeners.append(listener6)
        effective_port = listener6.getsockname()[1]
        listeners.append(socket.create_server(('0.0.0.0' if settings['ipv4'] else '127.0.0.1',effective_port)))
        server = create_server(application,sockets=listeners,threads=32,channel_timeout=240)
    except OSError:
        for listener in listeners:
            listener.close()
        raise
    server.effective_port = effective_port
    if manager:
        manager.attach(effective_port,[(s.getsockname()[0],effective_port) for s in listeners])
    return server


class NetworkError(Exception):
    pass


class NetworkSettings:
    def __init__(self, storage=None):
        self.storage, self.lock = storage,RLock()
        self.settings = (storage.load_network_settings() if storage is not None else None) or {'ipv4':True,'ipv6':True}
        self.port, self.bindings, self.active, self.override = None,[],None,''

    def attach(self, port, bindings, *, override=''):
        with self.lock:
            self.port, self.bindings, self.override = int(port),list(bindings),override
            self.active = {'ipv4':any(host=='0.0.0.0' for host,_ in bindings),
                           'ipv6':any(host=='::' for host,_ in bindings)}

    def update(self, payload):
        if (not isinstance(payload,dict) or set(payload) != {'ipv4','ipv6'}
                or any(not isinstance(payload[key],bool) for key in ('ipv4','ipv6'))):
            raise NetworkError('请选择是否开启 IPv4、IPv6 对外监听。')
        with self.lock:
            if self.override:
                raise NetworkError('当前监听由环境或手机版控制，无法在此修改。')
            if self.storage is not None:
                self.storage.save_network_settings(payload)
            self.settings = dict(payload)

    def status(self, fallback_port=5500):
        with self.lock:
            port,active = self.port or fallback_port,self.active
            result = {'settings':dict(self.settings),'active':dict(active) if active is not None else None,
                'port':port,'managed':not self.override,
                'restart_required':active is not None and not self.override and self.settings != active,
                'bindings':[{'host':host,'port':p} for host,p in self.bindings],
                'local':[f'http://127.0.0.1:{port}/',f'http://[::1]:{port}/'],'ipv4':[],'ipv6':[]}
            if self.override == 'android':
                result['local'] = [f'http://127.0.0.1:{port}/']
        addresses = set()
        try:
            for record in socket.getaddrinfo(socket.gethostname(),None,type=socket.SOCK_STREAM):
                addresses.add(ip_address(record[4][0].split('%',1)[0]))
        except socket.gaierror:
            result['address_warning'] = '暂时无法读取网卡地址，请刷新后重试。'
        for address in sorted(addresses,key=lambda value:(value.version,int(value))):
            if address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
                continue
            key = 'ipv4' if address.version == 4 else 'ipv6'
            result[key].append(f'http://{str(address) if address.version==4 else "["+str(address)+"]"}:{port}/')
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
                       message='监听设置已保存，重启服务后生效。')
    return manager
