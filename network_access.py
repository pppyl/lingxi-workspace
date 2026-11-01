"""Host validation and a shared IPv4/IPv6 listener."""
from ipaddress import ip_address
import os
import re
import socket
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
    host = os.environ.get('LINGXI_HOST')
    if host:
        return create_server(application, host=host, port=port, threads=32, channel_timeout=240)
    # One dual-stack socket also guarantees the same port when port=0.
    listener = socket.create_server(('::', port), family=socket.AF_INET6, dualstack_ipv6=True)
    return create_server(application, sockets=[listener], threads=32, channel_timeout=240)
