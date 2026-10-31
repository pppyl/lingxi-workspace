"""Host validation shared by the workspace and mounted activity app."""
from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit


LAN_NETWORKS = tuple(ip_network(value) for value in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))


def allowed_lan_host(host):
    if any(char in host for char in '/?#@\\'):
        return False
    try:
        parsed = urlsplit('//'+host)
        hostname, port = parsed.hostname, parsed.port
        if hostname == 'localhost':
            return True
        address = ip_address(hostname)
    except ValueError:
        return False
    return address.is_loopback or any(address in network for network in LAN_NETWORKS)
