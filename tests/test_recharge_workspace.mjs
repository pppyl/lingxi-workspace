import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as M from '../recharge/static/task-model.js';

class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.handlers={};this.dataset={};this.attrs={};this.hidden=false;this.disabled=false;this.checked=false;this.value='';this.classList={toggle(){}};}
  addEventListener(name,fn){this.handlers[name]=fn;}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;}
  setAttribute(name,value){this.attrs[name]=value;}
  showModal(){this.open=true;}
  close(){this.open=false;}
  click(){if(!this.disabled)return this.handlers.click?.();}
}
function find(node,text){if(node.textContent===text)return node;for(const child of node.children||[]){const found=find(child,text);if(found)return found;}return null;}
async function fixture({running=false}={}){
  const elements=new Map(),calls=[],listeners={},get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const filters=['all','scheduled','running','attention','ended'].map(v=>{const b=new Element('button');b.dataset.wedFilter=v;return b;});
  const pool=[{id:'a',name:'主号',phone_masked:'138****0001',status:'active'},{id:'b',name:'备用',phone_masked:'138****0002',status:'active'},{id:'c',name:'待登录',phone_masked:'138****0003',status:'login_required'}];
  const accounts=pool.map(a=>({id:'activity-'+a.id,login_id:a.id,name:a.name,phone:a.phone_masked,authenticated:false,activity:{}}));
  const jobs=Object.fromEntries(pool.slice(0,2).map(a=>['activity-'+a.id,{account_id:'activity-'+a.id,managed:true,enabled:true,state:running?'armed':'waiting',weekly:true,next_run_at:'2026-10-28T08:00:00+08:00',run_at:'2026-10-28T08:00:00+08:00',max_attempts:20,attempts:0,submissions:0,message:'每周三自动执行',history:[],updated_at:'2026-10-26T10:00:00+08:00'}]));
  let url='http://localhost:5500/recharge/';
  const fetch=async(path,options)=>{const body=options?.body?JSON.parse(options.body):undefined;calls.push({path,body});let result;
    if(path==='/recharge/api/accounts')result={accounts,logins:pool,jobs,server_time:'2026-10-26T10:00:00+08:00'};
    else if(path==='/recharge/api/login/attach')result={account_id:'activity-'+body.login_id};
    else if(path.startsWith('/recharge/api/prizes?'))result={prizes:[{prizeName:'fixture',sendStatus:'UNUSED'}]};
    else if(path==='/recharge/api/schedules/cancel'){for(const id of body.account_ids)jobs[id]={...jobs[id],skip_week:'2026-10-28',next_run_at:'2026-11-04T08:00:00+08:00'};result={message:'已停止'};}
    else throw new Error('Unexpected request '+path);
    return {ok:true,json:async()=>structuredClone(result)};
  };
  const source=fs.readFileSync(new URL('../recharge/static/workspace.js',import.meta.url),'utf8').replace(/^import \* as M from .+;\r?\n/,'');
  vm.runInNewContext(source,{M,document:{getElementById:get,createElement:tag=>new Element(tag),documentElement:{dataset:{apiBase:'/recharge'}},querySelector:()=>({content:'fixture-csrf'}),querySelectorAll:selector=>selector==='[data-wed-filter]'?filters:[],hidden:false},window:{addEventListener:(n,fn)=>listeners[n]=fn},fetch,URL,URLSearchParams,Intl,Date,location:{href:url,search:''},history:{replaceState(_state,_title,next){url=String(next);}},setInterval(){}});
  await new Promise(resolve=>setImmediate(resolve));
  return {get,calls,filters,accounts,jobs,url:()=>url};
}

test('opening Wednesday uses automatic backend tasks and never creates a manual schedule',async()=>{
  const {get,calls}=await fixture();
  assert.equal(get('wed-accounts').children.length,4);
  assert.equal(get('wed-status-rows').children.length,3);
  assert.equal(get('wed-task-rows').children.length,2);
  assert.equal(calls.length,1);assert.equal(calls[0].path,'/recharge/api/accounts');
  assert.equal(calls.some(c=>c.body),false);
});
test('activity and tasks are tabs on one page with no navigation or extra account login',async()=>{
  const {get,calls,url}=await fixture();
  get('wed-tab-tasks').click();
  assert.equal(get('wed-tasks').hidden,false);assert.equal(get('wed-activity').hidden,true);
  assert.equal(url(),'http://localhost:5500/recharge/?tab=tasks');
  get('wed-tab-activity').click();assert.equal(get('wed-activity').hidden,false);
  assert.equal(calls.length,1);
});
test('account cards scope both tables and show the selected account prizes',async()=>{
  const {get,calls}=await fixture();
  get('wed-accounts').children[1].click();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(get('wed-status-rows').children.length,1);
  assert.equal(get('wed-task-rows').children.length,1);
  assert.match(get('wed-prize-owner').textContent,/主号/);
  assert.equal(get('wed-prizes').children.length,1);
  assert.equal(calls.filter(c=>c.path==='/recharge/api/login/attach').length,1);
  assert.equal(calls.some(c=>c.path==='/recharge/api/schedules'||c.path==='/recharge/api/claim'),false);
});
test('expired account is shown with relogin and does not attempt another business login or claim',async()=>{
  const {get,calls}=await fixture();
  get('wed-accounts').children[3].click();
  assert.equal(calls.length,1);
  const relogin=find(get('wed-status-rows'),'重新登录');
  assert.ok(relogin);assert.equal(relogin.dataset.accountRelogin,'c');
});
test('batch stop applies only to checked rows and preserves automatic service for next week',async()=>{
  const {get,calls}=await fixture();
  get('wed-tab-tasks').click();get('wed-select-all').checked=true;get('wed-select-all').handlers.change();
  assert.equal(get('wed-selected-count').textContent,'已选 2 项');
  get('wed-stop-selected').click();assert.equal(get('wed-stop').open,true);
  await get('wed-stop-form').handlers.submit({preventDefault(){}});
  const stop=calls.find(c=>c.path==='/recharge/api/schedules/cancel');assert.deepEqual(stop.body.account_ids.sort(),['activity-a','activity-b']);
  assert.equal(get('wed-stop').open,false);assert.match(get('wed-feedback').textContent,/下周三会自动恢复/);
  assert.equal(get('wed-select-all').disabled,true);
  assert.equal(calls.some(c=>c.path==='/recharge/api/claim'),false);
});
test('manual status queries stay disabled while automatic preparation is armed',async()=>{
  const {get,calls}=await fixture({running:true});
  const read=find(get('wed-status-rows'),'查询资格');assert.equal(read.disabled,true);read.click();
  assert.equal(calls.length,1);
});
