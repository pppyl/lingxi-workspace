from pathlib import Path
import socket
import tempfile
from unittest import TestCase, main
from unittest.mock import patch

from app import create_app
from login_storage import DatabaseLoginStorage
from network_access import NetworkError, NetworkSettings, create_http_server


class NetworkSettingsTests(TestCase):
    def test_settings_are_persistent_and_only_change_bindings_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = DatabaseLoginStorage(Path(folder)/'accounts.sqlite3')
            manager = NetworkSettings(storage)
            manager.attach(5500,[('::',5500),('0.0.0.0',5500)])
            manager.update({'ipv4':False,'ipv6':True})
            self.assertEqual(manager.active,{'ipv4':True,'ipv6':True})
            self.assertTrue(manager.status()['restart_required'])
            self.assertEqual(NetworkSettings(storage).settings,{'ipv4':False,'ipv6':True})

    def test_real_listeners_support_each_independent_family_combination(self):
        import os
        for ipv4,ipv6 in ((True,True),(True,False),(False,True),(False,False)):
            with self.subTest(ipv4=ipv4,ipv6=ipv6):
                app = create_app()
                manager = app.extensions['network_settings']
                manager.update({'ipv4':ipv4,'ipv6':ipv6})
                with patch.dict(os.environ):
                    os.environ.pop('LINGXI_HOST',None)
                    server = create_http_server(app,0)
                try:
                    bindings = manager.status()['bindings']
                    self.assertEqual({binding['host'] for binding in bindings},
                        {'0.0.0.0' if ipv4 else '127.0.0.1','::' if ipv6 else '::1'})
                    self.assertEqual({binding['port'] for binding in bindings},{int(server.effective_port)})
                    self.assertFalse(manager.status()['restart_required'])
                finally:
                    server.close()

    def test_discovery_filters_link_local_and_brackets_ipv6_addresses(self):
        records = [(socket.AF_INET,socket.SOCK_STREAM,0,'',('192.168.31.176',0)),
            (socket.AF_INET6,socket.SOCK_STREAM,0,'',('2409:8a6a::1234',0,0,0)),
            (socket.AF_INET6,socket.SOCK_STREAM,0,'',('fe80::1%12',0,0,12)),
            (socket.AF_INET,socket.SOCK_STREAM,0,'',('127.0.0.1',0))]
        manager = NetworkSettings()
        with patch('network_access.socket.getaddrinfo',return_value=records):
            value = manager.status(5500)
        self.assertEqual(value['ipv4'],['http://192.168.31.176:5500/'])
        self.assertEqual(value['ipv6'],['http://[2409:8a6a::1234]:5500/'])

    def test_invalid_settings_and_environment_managed_listener_cannot_be_saved(self):
        manager = NetworkSettings()
        for value in ([],None,{'ipv4':'false','ipv6':True},{'ipv4':True},{'ipv4':True,'ipv6':True,'port':1}):
            with self.subTest(value=value), self.assertRaises(NetworkError):
                manager.update(value)
        manager.attach(5500,[('127.0.0.1',5500)],override='environment')
        with self.assertRaises(NetworkError):
            manager.update({'ipv4':False,'ipv6':False})

    def test_network_api_respects_remote_password_and_same_origin_rules(self):
        app = create_app()
        client = app.test_client()
        remote = {'base_url':'http://192.168.31.176:5500','environ_overrides':{'REMOTE_ADDR':'192.168.31.90'}}
        self.assertEqual(client.get('/api/network/status',**remote).status_code,428)
        self.assertEqual(client.post('/api/network/settings',json={'ipv4':False,'ipv6':False},**remote).status_code,428)
        self.assertEqual(client.post('/api/network/settings',json={'ipv4':False,'ipv6':True},
            headers={'Origin':'http://evil.test'}).status_code,403)
        response = client.post('/api/network/settings',json={'ipv4':False,'ipv6':True})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['settings'],{'ipv4':False,'ipv6':True})
        with client.get('/platforms') as page:
            self.assertIn('network-settings-form',page.get_data(as_text=True))


if __name__ == '__main__':
    main()
