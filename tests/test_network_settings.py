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
            self.assertEqual(NetworkSettings(storage).settings,{'ipv4':False,'ipv6':True,'custom':False,'addresses':[]})

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
        records = [{'address':v,'interface_id':'nic-a','interface_name':'测试网卡'}
                   for v in ('192.168.31.176','2409:8a6a::1234','fe80::1','127.0.0.1')]
        manager = NetworkSettings()
        with patch('network_interfaces._windows_addresses',return_value=records):
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
        self.assertEqual(response.json['settings'],{'ipv4':False,'ipv6':True,'custom':False,'addresses':[]})
        with client.get('/platforms') as page:
            self.assertIn('network-settings-form',page.get_data(as_text=True))

    def test_custom_listener_accepts_selected_ip_and_refuses_other_local_ips(self):
        import os,threading
        from urllib.request import build_opener,ProxyHandler
        from network_access import local_addresses
        addresses = [str(address) for address in local_addresses() if address.version == 4]
        if len(addresses) < 2:
            self.skipTest('This check needs two local IPv4 interfaces')
        app = create_app()
        manager = app.extensions['network_settings']
        manager.update({'ipv4':True,'ipv6':True,'custom':True,'addresses':[addresses[0]]})
        with patch.dict(os.environ):
            os.environ.pop('LINGXI_HOST',None)
            server = create_http_server(app,0)
        stopped = threading.Event()
        def serve():
            while not stopped.is_set():
                server.asyncore.loop(timeout=.05,count=1,map=server.map,use_poll=server.adj.asyncore_use_poll)
        worker = threading.Thread(target=serve,daemon=True)
        worker.start()
        try:
            value = manager.status()
            self.assertEqual({row['host'] for row in value['bindings']},{'::1','127.0.0.1',addresses[0]})
            self.assertEqual(value['active'],{'ipv4':True,'ipv6':False})
            self.assertFalse(value['restart_required'])
            self.assertEqual(value['listening']['ipv4'],[f'http://{addresses[0]}:{server.effective_port}/'])
            opener = build_opener(ProxyHandler({}))
            for host in (addresses[0],'127.0.0.1','[::1]'):
                with opener.open(f'http://{host}:{server.effective_port}/api/access/status',timeout=5) as response:
                    self.assertEqual(response.status,200)
            with socket.socket() as connection:
                connection.settimeout(.5)
                self.assertNotEqual(connection.connect_ex((addresses[1],int(server.effective_port))),0)
        finally:
            stopped.set()
            worker.join(timeout=2)
            server.close()

    def test_missing_selected_address_keeps_loopback_without_wildcard_fallback(self):
        import os
        app = create_app()
        manager = app.extensions['network_settings']
        manager.update({'ipv4':True,'ipv6':True,'custom':True,'addresses':['192.0.2.222']})
        with patch.dict(os.environ),patch('network_access.interface_addresses',return_value=[]):
            os.environ.pop('LINGXI_HOST',None)
            server = create_http_server(app,0)
            try:
                value = manager.status()
                self.assertEqual({row['host'] for row in value['bindings']},{'::1','127.0.0.1'})
                self.assertEqual(value['unavailable'],['192.0.2.222'])
                self.assertEqual(value['active'],{'ipv4':False,'ipv6':False})
                self.assertFalse(value['restart_required'])
            finally:
                server.close()

    def test_custom_selection_survives_storage_and_legacy_family_only_updates(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = DatabaseLoginStorage(Path(folder)/'accounts.sqlite3')
            storage.save_network_settings({'ipv4':True,'ipv6':False})
            manager = NetworkSettings(storage)
            self.assertFalse(manager.settings['custom'])
            manager.update({'ipv4':True,'ipv6':True,'custom':True,
                            'addresses':['2001:DB8::1','192.168.31.176','2001:db8::1']})
            manager = NetworkSettings(storage)
            manager.update({'ipv4':False,'ipv6':True})
            self.assertTrue(manager.settings['custom'])
            self.assertEqual(manager.settings['addresses'],['192.168.31.176','2001:db8::1'])

    def test_custom_validation_rejects_wildcards_non_ip_and_mutable_loopback(self):
        manager = NetworkSettings()
        for value in ('0.0.0.0','::','127.0.0.1','::1','fe80::1','224.0.0.1','::ffff:192.168.1.1','example.com',3):
            with self.subTest(value=value),self.assertRaises(NetworkError):
                manager.update({'ipv4':True,'ipv6':True,'custom':True,'addresses':[value]})
        manager.update({'ipv4':True,'ipv6':True,'custom':True,'addresses':[]})
        self.assertTrue(manager.settings['custom'])
        self.assertEqual(manager.settings['addresses'],[])


if __name__ == '__main__':
    main()
