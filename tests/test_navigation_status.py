from copy import deepcopy
from datetime import datetime
from threading import RLock
from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock, patch

from flask import Flask
from navigation_status import failure_counts, register_navigation_status
from recharge.cmcc import SHANGHAI

NOW = datetime(2026, 11, 6, 12, tzinfo=SHANGHAI)
ACCOUNTS = [{'id':'a','status':'active'}, {'id':'b','status':'login_required'}]


class NavigationCountTests(unittest.TestCase):
    def count(self, **snapshots):
        return failure_counts(ACCOUNTS, snapshots, NOW)

    def test_lingxi_counts_failed_rows_and_partial_results_but_not_stopped_or_removed_accounts(self):
        tasks = {str(i): {'login_id':login,'state':state} for i,(login,state) in enumerate([
            ('a','failed'),('a','partial'),('a','interrupted'),('a','cancelled'),
            ('a','waiting'),('a','completed'),('deleted','failed')])}
        self.assertEqual(self.count(lingxi=tasks)['/lingxi/tasks'],3)
        self.assertEqual(self.count()['accounts'],1)

    def test_daily_signin_confirmation_overrides_stale_failure_without_hiding_other_failed_tasks(self):
        data = {'tasks':{
            'sign':{'login_id':'a','kind':'signin','enabled':True,'state':'failed'},
            'flash':{'login_id':'a','kind':'flash','enabled':True,'state':'failed'},
            'stopped':{'login_id':'b','kind':'signin','enabled':False,'state':'failed'}},
            'observations':{'a':{'date':'20261106','today_state':'signed','flash_state':'ready'}}}
        self.assertEqual(self.count(checkin=data)['/checkin'],1)
        data['observations']['a']['flash_state']='claimed'
        self.assertEqual(self.count(checkin=data)['/checkin'],0)
        data['observations']['a']['date']='20261105'
        self.assertEqual(self.count(checkin=data)['/checkin'],2)

    def test_monthly_counts_one_account_despite_multiple_errors_and_ignores_completed_or_removed_rows(self):
        data = {'accounts':{
            'a':{'auto_enabled':True,'query_state':'failed','query_error':'timeout',
                 'observation':{'month':'202611','direct_state':'failed','bonus_state':'failed'}},
            'b':{'auto_enabled':True,'query_state':'failed','completed_date':'20261106'},
            'deleted':{'auto_enabled':True,'query_state':'failed'}}}
        self.assertEqual(self.count(monthly=data)['/monthly-coupon'],1)
        data['accounts']['a']={'auto_enabled':True,'observation':{'month':'202611','direct_state':'claimed','bonus_state':'pending'}}
        self.assertEqual(self.count(monthly=data)['/monthly-coupon'],0)

    def test_star_counts_unresolved_accounts_once_and_ignores_old_month_or_completed_auth_failures(self):
        data = {'accounts':{
            'a':{'query_state':'auth_failed','observation':{'date':'20261106','today_state':'signed','month_claimed':True}},
            'b':{'query_state':'rate_limited','observation':{}}},
            'jobs':{'j':{'state':'waiting','results_month':'202611','results':{'b':{'state':'failed'},'a':{'state':'failed'}}}}}
        self.assertEqual(self.count(star=data)['/star-day'],1)
        data['accounts']['b']['query_state']='waiting_session'
        data['jobs']['j']['results_month']='202610'
        self.assertEqual(self.count(star=data)['/star-day'],0)

    def test_coupon_badge_never_treats_paid_accepted_expired_or_pending_payment_as_failed(self):
        rows={str(i):{'login_id':'a','state':state} for i,state in enumerate([
            'accepted','closed','payment_required','submitted','unknown'])}
        self.assertEqual(self.count(coupons=rows)['/coupons'],1)
        rows['4']['state']='accepted'
        self.assertEqual(self.count(coupons=rows)['/coupons'],0)

    def test_counting_does_not_modify_snapshot_or_error_history(self):
        data={'lingxi':{'one':{'login_id':'a','state':'failed','attempts':[{'code':'1'}]}}}
        original=deepcopy(data)
        self.count(**data)
        self.assertEqual(data,original)

    def test_endpoint_aggregates_active_plugins_and_returns_only_counts(self):
        app=Flask('local-counts')
        counter=Mock(return_value=4)
        entry={'home':'/sample','navigation':'checkin','stopped':False,
            'controller':SimpleNamespace(metadata=lambda:{'active':True},scheduler=SimpleNamespace(failure_count=counter))}
        stopped={**entry,'stopped':True,'home':'/expired'}
        platforms=SimpleNamespace(lock=RLock(),platforms={'one':entry,'old':stopped})
        source=SimpleNamespace(lock=RLock(),jobs={'x':{'login_id':'a','state':'failed','secret':'never-expose'}})
        register_navigation_status(app,lambda:ACCOUNTS,{'lingxi':(source,'jobs')},platforms)
        result=app.test_client().get('/api/navigation/issues').get_json()
        self.assertEqual(result['counts']['/sample'],4)
        self.assertEqual(result['counts']['/checkin'],4)
        self.assertNotIn('/expired',result['counts'])
        self.assertNotIn('never-expose',json.dumps(result))
        self.assertNotIn('jobs',result)
        counter.assert_called_once()

    def test_workspace_api_never_calls_business_status_login_or_network_and_respects_remote_password(self):
        from app import create_app
        app=create_app();client=app.test_client()
        pool=app.extensions['unified_logins']
        pool.remember('18200000001','UID=private; JSESSIONID=private',{},'private-token',name='private-name')
        with (patch.object(app.extensions['checkin_scheduler'],'status',side_effect=AssertionError('would refresh')),
              patch.object(app.extensions['monthly_coupon_scheduler'],'status',side_effect=AssertionError('would refresh')),
              patch('app_login.AppLoginSession.call',side_effect=AssertionError('would authenticate')),
              patch('http_transport.requests.Session',side_effect=AssertionError('would request upstream'))):
            first=client.get('/api/navigation/issues')
            second=client.get('/api/navigation/issues')
        self.assertEqual(first.status_code,200)
        self.assertEqual(first.json['counts'],second.json['counts'])
        self.assertNotIn('private',first.get_data(as_text=True))
        remote={'base_url':'http://192.168.31.176:5500','environ_overrides':{'REMOTE_ADDR':'192.168.31.90'}}
        self.assertEqual(client.get('/api/navigation/issues',**remote).status_code,428)


if __name__ == '__main__':
    unittest.main()
