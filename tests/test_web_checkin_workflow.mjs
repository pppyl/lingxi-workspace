import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.handlers={};this.value='';this.textContent='';this.hidden=false;this.disabled=false;this.checked=false;this.dataset={};this.classList={toggle(){}};}
  set innerHTML(value){throw new Error('Unexpected HTML insertion: '+value);}
  addEventListener(name,handler){this.handlers[name]=handler;}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  setAttribute(){}
  showModal(){this.open=true;}
  close(){this.open=false;}
  click(){if(!this.disabled)return this.handlers.click?.();}
}
const walk=(element,text)=>element.textContent===text?element:element.children.map(child=>walk(child,text)).find(Boolean);
const flush=async()=>{for(let i=0;i<12;i++)await new Promise(resolve=>setImmediate(resolve));};
const script=fs.readFileSync(new URL('../activities/checkin_common/static/web-checkin.js',import.meta.url),'utf8');

async function fixture({accountCount=0,pauseRuns=false,wechat=false}={}){
  const elements=new Map(),calls=[],timers=[],pending=[],events={};
  const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  get('web-checkin-filter').value='all';
  const model={settings:{enabled:true,time:'08:00'},server_time:'2026-11-01T09:00:00+08:00',warning:'',accounts:[
    {id:'a',label:'正常号',phone_masked:'138****0001',status:'active',activity_state:'ready',today_signed:false,marked_times:1,total_times:3,records:[]},
    {id:'b',label:'待渠道登录',phone_masked:'138****0002',status:'active',activity_state:'login_required',today_signed:null,marked_times:null,total_times:null,records:[]},
    {id:'c',label:'今日已完成',phone_masked:'138****0003',status:'active',activity_state:'ready',today_signed:true,marked_times:2,total_times:3,records:[]},
    {id:'d',label:'共享已过期',phone_masked:'138****0004',status:'login_required',activity_state:'unknown',today_signed:null,records:[]},
  ]};
  if(accountCount)model.accounts=Array.from({length:accountCount},(_,i)=>({...model.accounts[0],id:'active-'+i}));
  const control={error:'',nonJson:false,now:100000,pauseRuns,active:0,maximum:0};
  class Clock extends Date {static now(){return control.now;}}
  const fetch=async(url,options)=>{
    const body=options?.body?JSON.parse(options.body):undefined;calls.push({url,body});
    if(wechat)url=url.replace('/api/checkin-wechat/','/api/checkin-web/');
    if(control.nonJson)return {ok:false,text:async()=>'<html>upstream error</html>'};
    if(control.error&&body)return {ok:false,text:async()=>JSON.stringify({error:control.error,retry_after:30})};
    if(url==='/api/checkin-web/status')return {ok:true,text:async()=>JSON.stringify(model)};
    if(url==='/api/checkin-web/run'){
      control.active++;control.maximum=Math.max(control.maximum,control.active);
      if(control.pauseRuns)await new Promise(resolve=>pending.push(resolve));
      control.active--;return {ok:true,text:async()=>JSON.stringify({message:'正在核对官方进度'})};
    }
    if(url==='/api/checkin-web/settings'){model.settings=body;return {ok:true,text:async()=>JSON.stringify({message:'设置已保存',settings:body})};}
    if(url==='/api/checkin-web/sms')return {ok:true,text:async()=>JSON.stringify({retry_after:60,message:'验证码已发送'})};
    if(url==='/api/checkin-web/login'){model.accounts.find(item=>item.id===body.id).activity_state='ready';return {ok:true,text:async()=>JSON.stringify({message:'登录成功'})};}
    if(url==='/api/checkin-web/authorize'){model.accounts.find(item=>item.id===body.id).activity_state='ready';return {ok:true,text:async()=>JSON.stringify({message:'微信授权已同步'})};}
    throw new Error('Unexpected endpoint '+url);
  };
  vm.runInNewContext(script,{document:{body:{dataset:{checkinChannel:wechat?'wechat':'web'}},getElementById:get,createElement:tag=>new Element(tag),querySelectorAll:()=>[],hidden:false},window:{addEventListener:(name,fn)=>{events[name]=fn;}},fetch,Date:Clock,setInterval(fn,ms){timers.push({fn,ms});}});
  await flush();return {get,calls,timers,events,model,control,pending};
}

test('WeChat page uses its own endpoints and never starts SMS or phone sync on opening or polling',async()=>{
  const {calls,get,timers}=await fixture({wechat:true});
  assert.ok(calls.every(call=>call.url.startsWith('/api/checkin-wechat/')));
  assert.ok(walk(get('web-checkin-accounts'),'短信登录'));
  await timers.find(timer=>timer.ms===5000).fn();await flush();
  assert.equal(calls.some(call=>/sms|login$|authorize$/.test(call.url)),false);
});

test('WeChat SMS login sends once for the selected account and submits the entered code',async()=>{
  const {calls,get}=await fixture({wechat:true});
  await walk(get('web-checkin-accounts'),'短信登录').click();await flush();
  assert.equal(get('web-checkin-login').open,true);
  assert.deepEqual(calls.find(call=>call.url.endsWith('/sms')).body,{id:'b'});
  get('web-checkin-code').value='123456';
  await get('web-checkin-login-form').handlers.submit({preventDefault(){}});
  assert.deepEqual(calls.find(call=>call.url.endsWith('/login')).body,{id:'b',code:'123456'});
  assert.equal(get('web-checkin-login').open,false);
});

test('a signed account with an expired activity session can still use WeChat SMS login',async()=>{
  const {get,model,events}=await fixture({wechat:true});
  model.accounts[2].activity_login_required=true;
  events['accounts-changed']();await flush();
  assert.ok(walk(get('web-checkin-accounts').children[2],'短信登录'));
});

test('opening queries each valid account only through the independent read-only operation and never sends SMS',async()=>{
  const {calls,get}=await fixture();
  assert.deepEqual(calls.filter(call=>call.url==='/api/checkin-web/run').map(call=>call.body),[
    {id:'a',kind:'refresh'},{id:'b',kind:'refresh'},{id:'c',kind:'refresh'},
  ]);
  assert.equal(calls.some(call=>/sms|login$/.test(call.url)),false);
  assert.ok(calls.every(call=>call.url.startsWith('/api/checkin-web/')));
  assert.ok(walk(get('web-checkin-accounts'),'本渠道需要单独登录'));
  assert.equal(walk(get('web-checkin-accounts').children[2],'签到').disabled,true);
});

test('periodic refresh reads local state only and a manual refresh rechecks the official status',async()=>{
  const {calls,timers,get}=await fixture();calls.length=0;
  await timers.find(timer=>timer.ms===5000).fn();await flush();
  assert.deepEqual(calls.map(call=>call.url),['/api/checkin-web/status']);
  get('web-checkin-refresh').click();await flush();
  assert.equal(calls.filter(call=>call.url==='/api/checkin-web/run').length,3);
  assert.equal(calls.some(call=>call.body?.kind==='signin'),false);
});

test('initial read-only queries are limited to four concurrent requests',async()=>{
  const {pending,control,calls}=await fixture({accountCount:9,pauseRuns:true});
  assert.equal(control.active,4);assert.equal(pending.length,4);
  control.pauseRuns=false;pending.splice(0).forEach(resolve=>resolve());await flush();
  assert.equal(control.maximum,4);
  assert.equal(calls.filter(call=>call.url==='/api/checkin-web/run').length,9);
});

test('explicit SMS login sends once, preserves account binding and enforces the retry countdown',async()=>{
  const {get,calls,control,timers}=await fixture();
  await walk(get('web-checkin-accounts').children[1],'短信登录').click();await flush();
  assert.equal(get('web-checkin-login').open,true);
  assert.equal(get('web-checkin-login-account').textContent,'待渠道登录 · 138****0002');
  assert.deepEqual(calls.find(call=>call.url==='/api/checkin-web/sms').body,{id:'b'});
  assert.equal(get('web-checkin-sms').disabled,true);
  get('web-checkin-sms').click();await flush();
  assert.equal(calls.filter(call=>call.url==='/api/checkin-web/sms').length,1);
  control.now+=61000;timers.find(timer=>timer.ms===1000).fn();
  assert.equal(get('web-checkin-sms').disabled,false);
  get('web-checkin-code').value='123456';
  await get('web-checkin-login-form').handlers.submit({preventDefault(){}});
  assert.deepEqual(calls.find(call=>call.url==='/api/checkin-web/login').body,{id:'b',code:'123456'});
  assert.equal(get('web-checkin-code').value,'');assert.equal(get('web-checkin-login').open,false);
  assert.equal(calls.some(call=>call.url==='/api/logins'||call.url==='/api/checkin/status'),false);
});

test('automatic settings apply globally without including an account selection',async()=>{
  const {get,calls}=await fixture();get('web-checkin-settings-open').click();
  assert.equal(get('web-checkin-time').value,'08:00');
  get('web-checkin-time').value='07:35';get('web-checkin-enabled').checked=true;
  await get('web-checkin-settings-form').handlers.submit({preventDefault(){}});
  assert.deepEqual(calls.find(call=>call.url==='/api/checkin-web/settings').body,{enabled:true,time:'07:35'});
  assert.equal(get('web-checkin-auto-time').textContent,'每天 07:35 起');
  assert.equal(get('web-checkin-settings').open,false);
});

test('account labels and upstream messages remain plain text and attention filtering is useful',async()=>{
  const {model,get,timers}=await fixture();
  model.accounts[0].label='<img src=x onerror=alert(1)>';
  model.accounts[0].message='<script>wrong()</script>';
  await timers.find(timer=>timer.ms===5000).fn();await flush();
  assert.ok(walk(get('web-checkin-accounts'),model.accounts[0].label));
  assert.ok(walk(get('web-checkin-accounts'),model.accounts[0].message));
  get('web-checkin-filter').value='attention';get('web-checkin-filter').handlers.change();
  assert.equal(get('web-checkin-accounts').children.length,1);
  assert.ok(walk(get('web-checkin-accounts'),'待渠道登录'));
});

test('a rejected or malformed login keeps the dialog open and never reports login success',async()=>{
  const {get,control}=await fixture();await walk(get('web-checkin-accounts').children[1],'短信登录').click();
  control.error='验证码已过期';get('web-checkin-code').value='123456';
  await get('web-checkin-login-form').handlers.submit({preventDefault(){}});
  assert.equal(get('web-checkin-login').open,true);
  assert.equal(get('web-checkin-login-error').textContent,'验证码已过期');
  control.nonJson=true;
  await get('web-checkin-login-form').handlers.submit({preventDefault(){}});
  assert.equal(get('web-checkin-login').open,true);
  assert.equal(get('web-checkin-login-error').textContent,'服务未返回有效结果，请稍后刷新。');
});

test('an account management notification reads new local state without sending authentication or signature requests',async()=>{
  const {events,calls}=await fixture();calls.length=0;
  await events['accounts-changed']();await flush();
  assert.deepEqual(calls.map(call=>call.url),['/api/checkin-web/status']);
});

test('a completed sign-in cycle cannot be submitted again but still permits an official query',async()=>{
  const {model,get,timers,calls}=await fixture();
  Object.assign(model.accounts[0],{cycle_complete:true,marked_times:3,total_times:3,today_signed:false});
  Object.assign(model.accounts[2],{activity_state:'completed',marked_times:3,total_times:3,today_signed:true});
  await timers.find(timer=>timer.ms===5000).fn();await flush();
  const completed=get('web-checkin-accounts').children[0],signed=get('web-checkin-accounts').children[2];
  assert.ok(walk(completed,'本周期已完成'));assert.ok(walk(completed,'3 / 3'));
  assert.equal(walk(completed,'签到').disabled,true);
  assert.equal(walk(completed,'查询').disabled,false);
  assert.ok(walk(signed,'今日已签到'));
  assert.equal(walk(signed,'本周期已完成'),undefined);
  assert.equal(walk(signed,'签到').disabled,true);
  calls.length=0;await walk(completed,'查询').click();await flush();
  assert.deepEqual(calls.filter(call=>call.body),[{url:'/api/checkin-web/run',body:{id:'a',kind:'refresh'}}]);
  get('web-checkin-filter').value='unsigned';get('web-checkin-filter').handlers.change();
  assert.equal(walk(get('web-checkin-accounts'),'正常号'),undefined);
  get('web-checkin-filter').value='attention';get('web-checkin-filter').handlers.change();
  assert.equal(walk(get('web-checkin-accounts'),'今日已完成'),undefined);
});

test('completed activity state works when the separate cycle marker is unavailable',async()=>{
  const {model,get,timers}=await fixture();
  Object.assign(model.accounts[0],{activity_state:'completed',today_signed:false,marked_times:3,total_times:3});
  await timers.find(timer=>timer.ms===5000).fn();await flush();
  assert.ok(walk(get('web-checkin-accounts').children[0],'本周期已完成'));
  assert.equal(walk(get('web-checkin-accounts').children[0],'签到').disabled,true);
  get('web-checkin-filter').value='unsigned';get('web-checkin-filter').handlers.change();
  assert.equal(walk(get('web-checkin-accounts'),'正常号'),undefined);
});
