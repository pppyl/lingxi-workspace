"""Local activity state, account isolation and non-replayed team submissions."""
from copy import deepcopy
from datetime import datetime
import re
import secrets
from threading import Condition, RLock

from features.checkin.client import CheckinError
from recharge.cmcc import SHANGHAI
from .client import ENTRY
from .phone_call import sync_confirmed_call, sync_existing_authorization

SCOPE = 'native-group'


def team_sizes(count):
    """Prioritize ten-person teams, then fit the remaining five/three-person teams."""
    tens, rest = divmod(count, 10)
    candidates = [(rest - five * 5 - three * 3, -(five * 5 + three * 3), -five, five, three)
                  for five in range(rest // 5 + 1) for three in range(rest // 3 + 1)
                  if five * 5 + three * 3 <= rest]
    _, _, _, five, three = min(candidates)
    return [10] * tens + [5] * five + [3] * three


def clean(value):
    return re.sub(r'1[3-9]\d{9}', lambda match: match[0][:3] + '****' + match[0][-4:], str(value))[:600]


class GroupScheduler:
    def __init__(self, accounts, factory, storage, *, now=None):
        self.accounts, self.factory, self.storage = accounts, factory, storage
        self.now = now or (lambda: datetime.now(SHANGHAI))
        self.lock = RLock()
        self.condition = Condition(self.lock)
        self.batch_lock = RLock()
        self.account_locks, self.running, self.forgotten = {}, set(), set()
        self.stopped = False
        self.data = (storage.load_activity_data(SCOPE) if storage else None) or {'observations': {}, 'pending': {}, 'records': {},
            'campaign': {'startTime': '2026-11-24 00:00:00', 'endTime': '2026-12-31 23:59:59'}, 'rules': ''}
        self.data.setdefault('batch', None)
        self.data.setdefault('phone_calls', {})

    def _change(self, edit):
        with self.lock:
            value = deepcopy(self.data)
            edit(value)
            if self.storage:
                self.storage.save_activity_data(SCOPE, value)
            self.data = value

    def metadata(self):
        campaign = self.data['campaign']
        ended = self.now() > datetime.strptime(campaign['endTime'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=SHANGHAI)
        return {'title': 'APP 通话组队', 'ended': ended, 'active': not ended,
                'start_at': campaign['startTime'], 'end_at': campaign['endTime']}

    def status(self):
        with self.lock:
            return {'accounts': [{**account, 'observation': deepcopy(self.data['observations'].get(account['id'])),
                'pending': deepcopy(self.data['pending'].get(account['id'])), 'running': account['id'] in self.running,
                'phone_call': deepcopy(self.data['phone_calls'].get(account['id'])),
                'records': deepcopy(self.data['records'].get(account['id'], []))} for account in self.accounts()],
                'campaign': self.metadata(), 'rules': self.data['rules'],
                'call_authorization_available': bool(self.data.get('call_context'))}

    def _save_observation(self, login_id, observation):
        if login_id in self.forgotten:
            return
        observation = deepcopy(observation)
        observation['checked_at'] = self.now().isoformat()
        def save(data):
            data['observations'][login_id] = observation
            data['campaign'] = {key: observation['campaign'][key] for key in ('startTime', 'endTime')}
            group = observation['campaign'].get('userGroupInfo') or {}
            member = next((row for row in group.get('groupMembers', [])
                           if row.get('phone') == observation['identity']['phone']), {})
            call_done = str(member.get('callTaskStatus')) == '1'
            if call_done and login_id in data['phone_calls']:
                data['phone_calls'][login_id].update(stage='confirmed', checked_at=observation['checked_at'])
            pending = data['pending'].get(login_id)
            if pending:
                kind = pending['kind']
                confirmed = (kind in {'create', 'join'} and bool(group)
                    or kind == 'quit' and not group
                    or kind == 'claim' and str(observation['campaign'].get('groupPrizeBtnStatus')) in {'2', '3', '4'}
                    or kind == 'call_confirm' and call_done
                    or kind == 'call_authorize' and str(member.get('authTaskStatus')) == '1'
                    or kind == 'draw' and (
                        int(observation['campaign'].get('todayAlreadyDrawTimes') or 0) > pending['daily_draws']
                        or observation['campaign']['drawTimes'] < pending['draw_times']
                        or len(observation['prizes']) > pending['prize_count']))
                if confirmed:
                    data['pending'].pop(login_id, None)
        self._change(save)

    def run(self, login_id, kind='refresh', *, size=None, token=None,
            destination=None, call_id=None, connected=False):
        account = next((row for row in self.accounts() if row['id'] == login_id), None)
        if not account or account['status'] != 'active':
            raise CheckinError('请选择已登录的有效共享账号。', 'APP_LOGIN_REQUIRED', 401)
        with self.lock:
            gate = self.account_locks.setdefault(login_id, RLock())
        with gate:
            if self.stopped or self.metadata()['ended']:
                raise CheckinError('活动已停止或到期。', 'ACTIVITY_ENDED', 409)
            if kind == 'call_prepare' and (not isinstance(destination, str)
                    or not re.fullmatch(r'1[3-9][0-9]{9}', destination)):
                raise CheckinError('请输入要拨打的 11 位手机号码。', 'INVALID_CALL_NUMBER', 400)
            if kind == 'call_confirm':
                draft = self.data['phone_calls'].get(login_id)
                if (connected is not True or not isinstance(call_id, str) or not draft
                        or draft['id'] != call_id):
                    raise CheckinError('请确认本账号的电话已真实接通至少 5 秒。', 'REAL_CALL_CONFIRM_REQUIRED', 400)
            with self.lock:
                self.running.add(login_id)
            try:
                client = self.factory(login_id)
                with client.connect() as (session, snapshot):
                    observation = client.inspect(session)
                    self._save_observation(login_id, observation)
                    if kind == 'refresh':
                        if not self.data['rules']:
                            rules = client.rules()
                            self._change(lambda data: data.update(rules=rules))
                        return self.status()
                    if kind in {'call_prepare', 'call_confirm', 'call_authorize'}:
                        group = observation['campaign'].get('userGroupInfo') or {}
                        member = next((row for row in group.get('groupMembers', [])
                                       if row.get('phone') == observation['identity']['phone']), {})
                        if not member:
                            raise CheckinError('请先加入队伍，再准备本账号的通话任务。', 'CALL_TEAM_REQUIRED', 409)
                        if kind != 'call_authorize' and str(member.get('callTaskStatus')) == '1':
                            return self.status()
                        if str(member.get('authTaskStatus')) != '1':
                            if self.data['pending'].get(login_id):
                                raise CheckinError('上次提交结果待核实，请先刷新官方进度。', 'RESULT_UNCERTAIN', 409, uncertain=True)
                            if kind == 'call_confirm' or not self.data.get('call_context'):
                                raise CheckinError('本账号通话授权尚未获官方确认，请先同步既有授权并核对。', 'CALL_AUTH_REQUIRED', 409)
                            def before_authorization():
                                client._current(snapshot)
                                if self.stopped or login_id in self.forgotten:
                                    raise CheckinError('活动或账号已停止。', 'STOPPED', 409)
                                self._change(lambda data: data['pending'].__setitem__(login_id,
                                    {'kind': 'call_authorize', 'at': self.now().isoformat()}))
                            sync_existing_authorization(snapshot, self.data['call_context'],
                                                        before_submit=before_authorization)
                            observation = client.inspect(session)
                            self._save_observation(login_id, observation)
                            if self.data['pending'].get(login_id):
                                raise CheckinError('已同步既有授权，官方进度尚待确认，请刷新核对。', 'RESULT_UNCERTAIN', 409, uncertain=True)
                            self._record(login_id, 'success', '本账号既有通话授权已同步并获官方确认。')
                        if kind == 'call_authorize':
                            return self.status()
                    if self.data['pending'].get(login_id):
                        raise CheckinError('上次提交结果待核实，请先刷新官方进度。', 'RESULT_UNCERTAIN', 409, uncertain=True)
                    if kind == 'prepare':
                        client.prepare_device(snapshot)
                        self._record(login_id, 'success', '官方抽奖认证已准备，执行时将复用有效凭据。')
                        return self.status()
                    if kind == 'call_prepare':
                        if snapshot.get('primary_status') == 'login_required':
                            raise CheckinError('请先在账号管理恢复 APP 登录，再准备拨号。', 'APP_LOGIN_REQUIRED', 401)
                        draft = self.data['phone_calls'].get(login_id)
                        if not draft or draft['destination'] != destination or draft['stage'] != 'prepared':
                            draft = {'id': secrets.token_urlsafe(18), 'destination': destination,
                                     'stage': 'prepared', 'prepared_at': self.now().isoformat()}
                            self._change(lambda data: data['phone_calls'].__setitem__(login_id, draft))
                        return self.status()
                    if kind == 'call_confirm' and draft['stage'] != 'prepared':
                        raise CheckinError('这次通话已处理，请先核对官方进度；新通话请重新准备拨号。', 'CALL_ALREADY_SUBMITTED', 409)
                    def before_submit():
                        if self.stopped or login_id in self.forgotten:
                            raise CheckinError('活动或账号已停止。', 'STOPPED', 409)
                        pending = {'kind': kind, 'size': size, 'at': self.now().isoformat()}
                        if kind == 'draw':
                            pending.update(draw_times=observation['campaign']['drawTimes'],
                                daily_draws=int(observation['campaign'].get('todayAlreadyDrawTimes') or 0),
                                prize_count=len(observation['prizes']))
                        def save(data):
                            data['pending'][login_id] = pending
                            if kind == 'call_confirm':
                                data['phone_calls'][login_id].update(stage='submitted',
                                    confirmed_at=pending['at'], confirmation_source='operator_real_call')
                        self._change(save)
                    if kind == 'call_confirm':
                        client._current(snapshot)
                        result = sync_confirmed_call(snapshot, before_submit=before_submit)
                    else:
                        result = client.operate(session, snapshot, kind, observation, size=size, token=token,
                                                before_submit=before_submit)
                    if kind == 'invite':
                        self._record(login_id, 'success', '官方邀请链接已生成。')
                        return {'invitation': result, 'status': self.status()}
                    self._save_observation(login_id, client.inspect(session))
                    if self.data['pending'].get(login_id):
                        raise CheckinError('已提交，官方状态尚待确认，请刷新核对。', 'RESULT_UNCERTAIN', 409, uncertain=True)
                    self._record(login_id, 'success', {'create': '创建队伍已核对', 'join': '加入队伍已核对',
                        'quit': '退出队伍已核对', 'claim': '组队奖励已核对', 'draw': '本次抽奖次数及官方奖品记录已核对',
                        'call_confirm': '操作者确认真实通话，通话任务已获官方确认。'}[kind])
                    return self.status()
            except CheckinError as error:
                if getattr(error, 'definitive_rejection', False):
                    def reject(data):
                        data['pending'].pop(login_id, None)
                        if kind == 'call_confirm':
                            data['phone_calls'][login_id]['stage'] = 'rejected'
                    self._change(reject)
                elif self.data['pending'].get(login_id) and not error.uncertain:
                    error = CheckinError('提交结果尚待核实，请刷新官方进度。', 'RESULT_UNCERTAIN', 409, uncertain=True)
                self._record(login_id, 'pending' if error.uncertain else 'error', clean(error))
                raise error
            finally:
                with self.condition:
                    self.running.discard(login_id)
                    self.condition.notify_all()

    def _record(self, login_id, state, message):
        if login_id in self.forgotten:
            return
        def save(data):
            rows = data['records'].get(login_id, [])
            data['records'][login_id] = [{'at': self.now().isoformat(), 'state': state, 'message': message}, *rows][:30]
        self._change(save)

    def plan(self):
        batch = self.data['batch']
        if batch:
            return {'teams': deepcopy(batch['teams']), 'waiting': [], 'excluded': [], 'resume': True}
        eligible, excluded = [], []
        for account in self.accounts():
            if account['status'] != 'active':
                excluded.append({'id': account['id'], 'reason': '共享账号未登录'})
                continue
            try:
                self.run(account['id'])
                observation = self.data['observations'][account['id']]
                if observation['campaign'].get('userGroupInfo'):
                    excluded.append({'id': account['id'], 'reason': '已经在队伍中'})
                elif not observation['campaign'].get('showGroup'):
                    excluded.append({'id': account['id'], 'reason': '官方未开放资格'})
                elif self.data['pending'].get(account['id']):
                    excluded.append({'id': account['id'], 'reason': '上次提交待核实'})
                else:
                    eligible.append(account['id'])
            except CheckinError as error:
                excluded.append({'id': account['id'], 'reason': clean(error)})
        sizes, cursor, teams = team_sizes(len(eligible)), 0, []
        for size in sizes:
            teams.append({'size': size, 'members': eligible[cursor:cursor + size]})
            cursor += size
        return {'teams': teams, 'waiting': eligible[cursor:], 'excluded': excluded}

    def assemble(self, teams):
        with self.batch_lock:
            return self._assemble(teams)

    def _assemble(self, teams):
        if not isinstance(teams, list) or not teams:
            raise CheckinError('请先预览组队方案。', 'INVALID_PLAN', 400)
        active = {row['id'] for row in self.accounts() if row['status'] == 'active'}
        seen = set()
        for team in teams:
            if (not isinstance(team, dict) or type(team.get('size')) is not int or team['size'] not in {3, 5, 10}
                    or not isinstance(team.get('members'), list) or len(team['members']) != team['size']
                    or any(not isinstance(key, str) or key not in active or key in seen for key in team['members'])
                    or len(set(team['members'])) != len(team['members'])):
                raise CheckinError('组队方案或账号状态已变化，请重新预览。', 'INVALID_PLAN', 409)
            seen.update(team['members'])
        batch = self.data['batch']
        if batch and batch['teams'] != teams:
            raise CheckinError('上次组队尚未完成，请继续原方案以保留队伍归属。', 'BATCH_PENDING', 409)
        for key in seen:
            self.run(key)
            if self.data['pending'].get(key):
                raise CheckinError('方案内账号有结果待核实，请先刷新确认。', 'RESULT_UNCERTAIN', 409, uncertain=True)
            if not batch and self.data['observations'][key]['campaign'].get('userGroupInfo'):
                raise CheckinError('方案内号码已经入队，请重新预览。', 'PLAN_CHANGED', 409)
        if not batch:
            self._change(lambda data: data.update(batch={'teams': deepcopy(teams), 'at': self.now().isoformat()}))
        # Retain the exact confirmed plan across interruptions. Resume only
        # missing memberships, checking that existing members belong to this team.
        for team in teams:
            leader, *members = team['members']
            leader_group = self.data['observations'][leader]['campaign'].get('userGroupInfo')
            if not leader_group:
                self.run(leader, 'create', size=team['size'])
                leader_group = self.data['observations'][leader]['campaign']['userGroupInfo']
            allowed = {self.data['observations'][key]['identity']['phone'] for key in team['members']}
            phones = {member['phone'] for member in leader_group['groupMembers']}
            if leader_group['groupSize'] != team['size'] or not phones <= allowed:
                raise CheckinError('队伍归属或人数与确认方案不一致，已暂停。', 'PLAN_CHANGED', 409)
            invitation = None
            for key in members:
                existing = self.data['observations'][key]['campaign'].get('userGroupInfo')
                if existing:
                    existing_phones = {member['phone'] for member in existing['groupMembers']}
                    leader_phone = self.data['observations'][leader]['identity']['phone']
                    if (leader_phone not in existing_phones or existing['groupSize'] != team['size']
                            or not existing_phones <= allowed):
                        raise CheckinError('方案内号码已加入其他队伍，已暂停。', 'PLAN_CHANGED', 409)
                    continue
                if invitation is None:
                    invitation = self.run(leader, 'invite')['invitation']['url']
                self.run(key, 'join', token=invitation)
                self.run(leader)
                leader_group = self.data['observations'][leader]['campaign']['userGroupInfo']
                phones = {member['phone'] for member in leader_group['groupMembers']}
                if not phones <= allowed:
                    raise CheckinError('队伍出现方案外成员，已暂停后续组队。', 'PLAN_CHANGED', 409)
            if len(phones) != team['size']:
                raise CheckinError('队员入队结果尚待官方确认，请刷新后继续原方案。', 'RESULT_UNCERTAIN', 409, uncertain=True)
        self._change(lambda data: data.update(batch=None))
        return self.status()

    def busy_login(self, login_id):
        return login_id in self.running

    def forget(self, login_id):
        self.forgotten.add(login_id)
        def remove(data):
            for key in ('observations', 'pending', 'records', 'phone_calls'):
                data[key].pop(login_id, None)
        self._change(remove)

    def start(self):
        self.stopped = False

    def shutdown(self, wait=True):
        with self.condition:
            self.stopped = True
            if wait:
                self.condition.wait_for(lambda: not self.running)
