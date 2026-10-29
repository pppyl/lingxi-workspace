import test from 'node:test';
import assert from 'node:assert/strict';
import {beijingInput,planPayload,taskGroup,canBatchAdjust,batchPlanPayload,canDeleteTask,canStopTask,filterTasks} from '../static/lingxi-task-model.js';
const plan={loginIds:['unit'],content:' hello ',runAt:'2026-11-04T08:00:00'};
const now=Date.parse('2026-10-29T00:00:00+08:00');
test('default burst is three sends one second apart in Beijing time',()=>{
  const value=planPayload(plan,now);assert.equal(value.repeat_count,3);assert.equal(value.interval,1);
  assert.equal(value.run_at,'2026-11-04T08:00:00+08:00');assert.equal(value.content,'hello');
});
test('counts and fractional intervals are configurable',()=>{
  const value=planPayload({...plan,count:'5',interval:'0.5',weekly:true},now);
  assert.equal(value.repeat_count,5);assert.equal(value.interval,.5);assert.equal(value.weekly,true);
});
test('selecting twelve accounts keeps the complete batch',()=>{
  const ids=Array.from({length:12},(_,i)=>'account-'+i);
  const value=planPayload({...plan,loginIds:ids,count:3,interval:2},now);
  assert.deepEqual(value.login_ids,ids);assert.equal(value.repeat_count,3);assert.equal(value.interval,2);
});
test('date input remains Beijing time across host time zones',()=>{
  assert.equal(beijingInput('2026-11-04T00:00:00Z'),'2026-11-04T08:00:00');assert.equal(beijingInput('bad'),'');
});
test('invalid or past plans cannot be submitted',()=>{
  for(const change of [{loginIds:[]},{content:''},{count:'1.5'},{count:21},{interval:0},{interval:'NaN'},
    {runAt:'bad'},{runAt:'2026-01-01T00:00:00'}])assert.throws(()=>planPayload({...plan,...change},now));
});
test('streaming and stopping stay visible as active tasks',()=>{
  assert.equal(taskGroup({state:'receiving',enabled:false}),'active');
  assert.equal(taskGroup({state:'waiting',enabled:true}),'waiting');
  assert.equal(taskGroup({state:'cancelled',enabled:false}),'ended');
  assert.equal(taskGroup({state:'queued',enabled:true}),'active');
});
test('batch adjustment only selects enabled waiting jobs',()=>{
  assert.equal(canBatchAdjust({state:'waiting',enabled:true}),true);
  assert.equal(canBatchAdjust({state:'running',enabled:true}),false);
  assert.equal(canBatchAdjust({state:'cancelled',enabled:false}),false);
});
test('batch payload contains only explicitly selected fields',()=>{
  const value=batchPlanPayload(['one','two'],{interval:'2'},now);
  assert.deepEqual(value,{ids:['one','two'],changes:{interval:2}});
});
test('batch time is converted to Beijing timezone',()=>{
  const value=batchPlanPayload(['one'],{run_at:'2026-11-04T08:00:00',weekly:false},now);
  assert.equal(value.changes.run_at,'2026-11-04T08:00:00+08:00');assert.equal(value.changes.weekly,false);
});
test('empty and invalid batch changes are rejected',()=>{
  assert.throws(()=>batchPlanPayload([],{interval:1},now));
  for(const changes of [{},{repeat_count:21},{interval:'NaN'},{content:' '},{run_at:'2026-01-01T00:00:00'}])
    assert.throws(()=>batchPlanPayload(['one'],changes,now));
});

test('completed and partial jobs can be deleted while live streams must be stopped first',()=>{
  for(const state of ['completed','partial','failed','cancelled','waiting'])assert.equal(canDeleteTask({state}),true);
  for(const state of ['preparing','running','queued','receiving','stopping'])assert.equal(canDeleteTask({state}),false);
  assert.equal(canStopTask({enabled:false,state:'receiving'}),true);
  assert.equal(canStopTask({enabled:true,state:'waiting'}),true);
  assert.equal(canStopTask({enabled:false,state:'completed'}),false);
});

test('task filters combine group, exact status and account or content search',()=>{
  const rows=[
    {id:'one',enabled:false,state:'partial',account_name:'BB1621',phone_masked:'182****1621',content:'柑橘甜滋滋'},
    {id:'two',enabled:false,state:'completed',account_name:'BB6562',phone_masked:'188****6562',content:'柑橘甜滋滋'},
    {id:'three',enabled:true,state:'waiting',account_name:'BB1621',phone_masked:'182****1621',content:'其他内容'},
    {id:'four',enabled:true,state:'receiving',account_name:'BB7797',phone_masked:'182****7797',content:'柑橘甜滋滋'},
  ];
  assert.deepEqual(filterTasks(rows,{group:'ended',query:' 柑橘 '}).map(task=>task.id),['one','two']);
  assert.deepEqual(filterTasks(rows,{state:'partial',query:'bb 1621'}).map(task=>task.id),['one']);
  assert.deepEqual(filterTasks(rows,{group:'waiting',query:'1621'}).map(task=>task.id),['three']);
  assert.deepEqual(filterTasks(rows,{group:'ended',state:'receiving'}),[]);
  assert.deepEqual(filterTasks(rows,{query:'不存在'}),[]);
  assert.deepEqual(filterTasks(rows),rows);
});
