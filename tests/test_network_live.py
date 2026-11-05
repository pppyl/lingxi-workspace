from copy import deepcopy
from pathlib import Path
import os
import socket
import tempfile
from threading import Event, Thread
import time
from unittest import TestCase, main
from unittest.mock import Mock, patch
from urllib.request import ProxyHandler, build_opener

from flask import Flask
from login_storage import DatabaseLoginStorage
from network_access import NetworkSettings, create_http_server, local_addresses, register_network


def address(value, identity='selected', name='所选网卡'):
    return {'address':value,'interface_id':identity,'interface_name':name,
            'family':'ipv6' if ':' in value else 'ipv4'}


class AdapterSelectionTests(TestCase):
    def test_ipv4_ipv6_rotations_follow_same_adapter_even_on_a_new_subnet(self):
        manager=NetworkSettings()
        manager.update({'ipv4':True,'ipv6':True,'custom':True,'addresses':['192.168.31.10','2409:1::10']})
        manager.plan([address('192.168.31.10'),address('2409:1::10'),address('10.0.0.2','not-selected')])
        hosts=manager.plan([address('192.168.50.11'),address('2409:2::11'),address('10.0.0.3','not-selected')])
        self.assertEqual(set(hosts),{'::1','127.0.0.1','192.168.50.11','2409:2::11'})
        self.assertEqual(manager.settings['addresses'],['192.168.50.11','2409:2::11'])

    def test_disconnect_waits_for_same_adapter_and_never_opens_another_nic(self):
        manager=NetworkSettings()
        manager.update({'ipv4':True,'ipv6':True,'custom':True,'addresses':['192.168.31.10']})
        manager.plan([address('192.168.31.10')])
        self.assertEqual(manager.plan([address('192.168.31.11','other')]),['::1','127.0.0.1'])
        self.assertEqual(manager.plan([address('192.168.88.12')]),['::1','127.0.0.1','192.168.88.12'])

    def test_an_unchecked_existing_address_on_the_same_nic_is_not_selected_as_replacement(self):
        manager=NetworkSettings()
        manager.update({'ipv4':True,'ipv6':True,'custom':True,'addresses':['2409:1::10']})
        manager.plan([address('2409:1::10'),address('2409:1::20')])
        self.assertEqual(manager.plan([address('2409:1::20')]),['::1','127.0.0.1'])
        self.assertEqual(manager.plan([address('2409:1::20'),address('2409:2::30')]),['::1','127.0.0.1','2409:2::30'])

    def test_adapter_identity_and_exclusions_survive_restart_without_widening_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            storage=DatabaseLoginStorage(Path(folder)/'state.sqlite3')
            manager=NetworkSettings(storage)
            manager.update({'ipv4':True,'ipv6':True,'custom':True,'addresses':['2409:1::10']})
            manager.plan([address('2409:1::10'),address('2409:1::20')])
            manager=NetworkSettings(storage)
            self.assertEqual(manager.plan([address('2409:1::20'),address('2409:2::30')]),['::1','127.0.0.1','2409:2::30'])
            self.assertEqual(NetworkSettings(storage).settings['addresses'],['2409:2::30'])

    def test_legacy_missing_address_migrates_only_with_an_unambiguous_prefix_match(self):
        for candidates,expected in (([address('2409:8a6a:404:9684::1')],'2409:8a6a:404:9684::1'),
                ([address('2409:8a6a:404:9684::1'),address('2409:8a6a:404:2222::1','other')],None)):
            with self.subTest(expected=expected):
                storage=Mock()
                storage.load_network_settings.return_value={'ipv4':True,'ipv6':True,'custom':True,
                    'addresses':['2409:8a6a:404:c325::1']}
                manager=NetworkSettings(storage)
                hosts=manager.plan(candidates)
                self.assertEqual(hosts,['::1','127.0.0.1']+([expected] if expected else []))

    def test_new_missing_ip_is_not_guessed_as_another_existing_address(self):
        manager=NetworkSettings()
        manager.update({'ipv4':True,'ipv6':True,'custom':True,'addresses':['192.168.31.10']})
        self.assertEqual(manager.plan([address('192.168.31.20')]),['::1','127.0.0.1'])


class LiveListenerTests(TestCase):
    def setUp(self):
        available=[str(a) for a in local_addresses() if a.version==4]
        if len(available)<2:self.skipTest('Needs two assigned IPv4 addresses to simulate a NIC address change')
        self.first,self.second=available[:2]
        self.rows=[address(self.first),address(self.second,'not-selected')]
        self.inventory=patch('network_access.interface_addresses',side_effect=lambda:deepcopy(self.rows))
        self.inventory.start();self.addCleanup(self.inventory.stop)
        self.environment=patch.dict(os.environ)
        self.environment.start();os.environ.pop('LINGXI_HOST',None);self.addCleanup(self.environment.stop)
        self.started,self.release=Event(),Event()
        self.app=Flask('live-network')
        self.manager=register_network(self.app,None)
        self.manager.update({'ipv4':True,'ipv6':True,'custom':True,'addresses':[self.first]})
        self.app.add_url_rule('/',view_func=lambda:'ok')
        def hold():
            self.started.set();self.release.wait(5)
            return 'finished'
        self.app.add_url_rule('/hold',view_func=hold)
        self.server=create_http_server(self.app,0)
        self.worker=Thread(target=self.server.run,daemon=True)
        self.worker.start();self.addCleanup(self.stop)

    def stop(self):
        self.release.set();self.server.close();self.worker.join(2)

    def wait_for(self,predicate):
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(.02)
        self.fail('Live listener did not reconcile')

    def get(self,host,path='/'):
        with build_opener(ProxyHandler({})).open(f'http://{host}:{self.server.effective_port}{path}',timeout=4) as response:
            return response.read().decode()

    def test_live_address_change_keeps_inflight_request_loopbacks_and_worker_pool(self):
        responses=[]
        client=Thread(target=lambda:responses.append(self.get(self.first,'/hold')))
        client.start();self.assertTrue(self.started.wait(2))
        dispatcher=self.server.backend.task_dispatcher
        self.rows=[address(self.second)]
        # No page access or explicit refresh: the server's background scan
        # must notice this independently.
        self.wait_for(lambda:{h for h,_ in self.manager.bindings}=={'::1','127.0.0.1',self.second})
        self.assertEqual(self.get(self.second),'ok')
        self.assertEqual(self.get('127.0.0.1'),'ok')
        self.assertEqual(self.get('[::1]'),'ok')
        with socket.socket() as connection:
            connection.settimeout(.5)
            self.assertNotEqual(connection.connect_ex((self.first,self.server.effective_port)),0)
        self.release.set();client.join(3)
        self.assertEqual(responses,['finished'])
        self.assertTrue(self.worker.is_alive())
        self.assertIs(self.server.backend.task_dispatcher,dispatcher)
        self.assertFalse(self.manager.status()['restart_required'])

    def test_saving_settings_reconfigures_without_restart_or_touching_other_interfaces(self):
        self.manager.update({'ipv4':False,'ipv6':True,'custom':True,'addresses':[self.first]})
        self.wait_for(lambda:{h for h,_ in self.manager.bindings}=={'::1','127.0.0.1'})
        self.assertFalse(self.manager.status()['applying'])
        self.manager.update({'ipv4':True,'ipv6':True,'custom':True,'addresses':[self.second]})
        self.wait_for(lambda:{h for h,_ in self.manager.bindings}=={'::1','127.0.0.1',self.second})
        self.assertEqual(self.get(self.second),'ok')
        self.assertFalse(self.manager.status()['restart_required'])

    def test_retired_address_closes_idle_keepalive_connections(self):
        from http.client import HTTPConnection
        connection=HTTPConnection(self.first,self.server.effective_port,timeout=3)
        self.addCleanup(connection.close)
        connection.request('GET','/')
        self.assertEqual(connection.getresponse().read(),b'ok')
        old_socket=connection.sock
        self.rows=[address(self.second)]
        self.server.changed.set()
        self.wait_for(lambda:self.second in {h for h,_ in self.manager.bindings})
        self.assertEqual(old_socket.recv(1),b'')
        self.assertEqual(self.get(self.second),'ok')

    def test_switching_wildcard_and_custom_modes_applies_on_the_same_running_server(self):
        self.manager.update({'ipv4':True,'ipv6':True,'custom':False,'addresses':[self.first]})
        self.wait_for(lambda:{h for h,_ in self.manager.bindings}=={'::','0.0.0.0'})
        self.assertEqual(self.get(self.second),'ok')
        self.manager.update({'ipv4':True,'ipv6':True,'custom':True,'addresses':[self.first]})
        self.wait_for(lambda:{h for h,_ in self.manager.bindings}=={'::1','127.0.0.1',self.first})
        self.assertEqual(self.get(self.first),'ok')
        with socket.socket() as connection:
            connection.settimeout(.5)
            self.assertNotEqual(connection.connect_ex((self.second,self.server.effective_port)),0)
        self.manager.update({'ipv4':True,'ipv6':True,'custom':True,'addresses':[]})
        self.wait_for(lambda:{h for h,_ in self.manager.bindings}=={'::1','127.0.0.1'})
        self.assertEqual(self.get('127.0.0.1'),'ok')

    def test_bind_failure_is_reported_and_retried_without_wildcard_fallback(self):
        create=socket.create_server
        def fail_new(target,**kwargs):
            if target[0]==self.second:raise OSError('simulated address not ready')
            return create(target,**kwargs)
        with patch('network_access.socket.create_server',side_effect=fail_new):
            self.rows=[address(self.second)]
            self.server.changed.set()
            self.wait_for(lambda:bool(self.manager.warning))
            self.assertEqual({h for h,_ in self.manager.bindings},{'::1','127.0.0.1'})
            self.assertTrue(self.manager.status()['applying'])
            self.assertEqual(self.get('127.0.0.1'),'ok')
        self.server.changed.set()
        self.wait_for(lambda:self.second in {h for h,_ in self.manager.bindings})
        self.assertEqual(self.get(self.second),'ok')
        self.assertEqual(self.manager.warning,'')


if __name__=='__main__':main()
