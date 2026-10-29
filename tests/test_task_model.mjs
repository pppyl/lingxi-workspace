import {test} from 'node:test';
import assert from 'node:assert/strict';
import * as M from '../recharge/static/task-model.js';
const base = {state:'waiting', enabled:true, next_run_at:'2026-11-04T08:00:00+08:00', updated_at:'2026-10-28T08:00:00+08:00', weekly:true};
const account = {id:'a', name:'测试账号', phone:'138****0000', authenticated:true};
function row(id, fields = {}, a = account) { const job = {...base, ...fields}; return {id, job, account:{...a, id}, group:M.group(job,a), status:M.status(job,a)}; }

test('a weekly success remains scheduled for its next run', () => {
  const item = row('a',{state:'success'});
  assert.equal(item.group,'scheduled'); assert.equal(item.status,'已预约');
  assert.equal(M.labels[item.job.state],'领取成功'); assert.equal(M.stoppable(item.job),true);
});
test('missing login and interrupted jobs need attention', () => {
  assert.equal(row('a',{}, {...account,authenticated:false}).group,'attention');
  assert.equal(row('a',{}, {...account,authenticated:false}).status,'待恢复登录');
  for (const state of ['failed','interrupted','missed']) assert.equal(row('a',{state,enabled:false}).group,'attention');
});
test('automatic recovery and missing account binding have distinct labels', () => {
  assert.equal(row('a',{}, {...account,authenticated:false,session_status:'restoring'}).status,'正在自动恢复');
  assert.equal(row('a',{}, {...account,authenticated:false,session_status:'retry'}).status,'等待自动重连');
  assert.equal(row('a',{}, {...account,authenticated:false,session_status:'login_required'}).status,'需要短信验证');
  assert.equal(row('a',{}, {...account,authenticated:false,mode:'none'}).status,'待连接共享账号');
});
test('running and stopping tasks take precedence over an expired login', () => {
  for (const state of M.active) assert.equal(row('a',{state}, {...account,authenticated:false}).group,'running');
  assert.equal(M.stoppable({...base,state:'cancelling'}),false);
  assert.equal(M.stoppable({...base,state:'cancelled',enabled:false}),false);
});
test('counts partition all tasks without double counting weekly completions', () => {
  const list = [row('a'),row('b',{state:'running'}),row('c',{state:'failed',enabled:false}),row('d',{state:'success',enabled:false}),row('e',{state:'success'})];
  assert.deepEqual(M.counts(list),{all:5,scheduled:2,running:1,attention:1,ended:1});
});
test('filters search name, masked phone, repeat and current result', () => {
  const list = [row('a'),row('b',{state:'success',enabled:false}, {...account,name:'家人',phone:'139****8888'})];
  assert.deepEqual(M.visible(list,'all','8888','next').map(r=>r.id),['b']);
  assert.deepEqual(M.visible(list,'scheduled','每周三','next').map(r=>r.id),['a']);
  assert.deepEqual(M.visible(list,'ended','领取成功','next').map(r=>r.id),['b']);
  assert.equal(M.visible(list,'scheduled','家人','next').length,0);
});
test('time sorting puts running first, then future plans, then terminal jobs', () => {
  const list = [row('later',{next_run_at:'2026-11-11T08:00:00+08:00'}),row('done',{enabled:false,state:'success'}),row('sooner'),row('running',{state:'running'})];
  assert.deepEqual(M.visible(list,'all','','next').map(r=>r.id),['running','sooner','later','done']);
});
test('records whose accounts were removed remain visible', () => {
  const list = M.rows({accounts:[],jobs:{missing:base}});
  assert.equal(list.length,1); assert.equal(list[0].group,'attention'); assert.equal(list[0].account.name,'原账号已移除');
});
test('rebooking time always uses Beijing time independent of local timezone', () => {
  assert.equal(M.beijingInput('2026-11-04T00:00:00Z'),'2026-11-04T08:00:00');
  assert.equal(M.nextWednesday(Date.parse('2026-10-28T08:01:00+08:00')),'2026-11-04T08:00:00');
  assert.equal(M.nextWednesday(Date.parse('2026-10-28T07:59:00+08:00')),'2026-10-28T08:00:00');
  assert.equal(M.nextWednesday(Date.parse('2026-10-28T09:00:00+08:00'),'2026-10-21T10:00:00+08:00'),'2026-10-28T10:00:00');
});
test('rebooking rejects past dates, non-Wednesdays and unsafe retry settings', () => {
  const now = Date.parse('2026-10-28T08:00:00+08:00');
  const valid = {run_at:'2026-11-04T08:00:00',interval_min:0.2,interval_max:1,max_attempts:20};
  assert.equal(M.validate(valid,now),'');
  for (const fields of [{run_at:'2026-10-28T07:00:00'},{run_at:'2026-11-05T08:00:00'},{run_at:''},
    {interval_min:0.9,interval_max:0.3},{interval_max:2},{interval_min:NaN},{max_attempts:21},{max_attempts:1.1}])
    assert.notEqual(M.validate({...valid,...fields},now),'');
});
