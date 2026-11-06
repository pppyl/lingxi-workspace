import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor() { this.children=[];this.handlers={};this.value='';this.textContent='';this.dataset={};this.hidden=true;this.disabled=false;this.classList={toggle(){}}; }
  addEventListener(event,handler) { this.handlers[event]=handler; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children=items; }
  setAttribute() {}
  showModal() { this.open=true; }
  close() { this.open=false; }
  click() { if(!this.disabled)return this.handlers.click?.(); }
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const walk=(element,text)=>element.textContent===text?element:element.children.map(child=>walk(child,text)).find(Boolean);
const texts=element=>[element.textContent,...element.children.map(texts)].join(' ');
async function fixture(options={}) {
  const elements=new Map(),calls=[],events={},polls=[],timers=new Map(),get=id=>{
    if(!elements.has(id))elements.set(id,new Element());return elements.get(id);
  };
  get('gold-filter').value='all';
  const account={id:'a',name:'0314',phone_masked:'182****0314',status:'active',observation:{date:'2031-04-15',signed:false},
    records:[],next_run:'2031-04-16T00:00:01+08:00'};
  const model={campaign:{active:true,ended:false,end_at:'2031-04-30T23:59:59+08:00'},server_time:'2031-04-15T08:00:00+08:00',
    settings:{enabled:true,start_time:'00:00:01',repeat_count:5,interval_seconds:1},accounts:[account],warning:''};
  if(options.ended)model.campaign={...model.campaign,active:false,ended:true};
  if(options.accounts)model.accounts=options.accounts;
  const control={error:'',refreshError:options.refreshError||''};
  const fetch=async(url,fetchOptions)=>{
    const body=fetchOptions?.body?JSON.parse(fetchOptions.body):undefined;calls.push({url,body});
    if(options.beforeResponse)await options.beforeResponse({url,calls,events});
    let result,ok=true;
    if(url==='/api/daily-gold/status')result=model;
    else if(url==='/api/daily-gold/settings'){
      if(control.error){ok=false;result={error:control.error};}
      else {model.settings=body;result={message:'已保存'};}
    }else if(url==='/api/daily-gold/refresh'){
      if(control.refreshError){ok=false;result={error:control.refreshError};}
      else result={message:'正在查询'};
    }else if(url==='/api/daily-gold/run')result={message:'已加入执行队列'};
    else throw new Error('Unexpected API '+url);
    return {ok,status:ok?200:502,headers:{get:()=> 'application/json'},json:async()=>JSON.parse(JSON.stringify(result))};
  };
  const document={getElementById:get,createElement:()=>new Element(),hidden:false};
  vm.runInNewContext(fs.readFileSync(new URL('../static/daily-gold.js',import.meta.url),'utf8'),{
    document,fetch,Intl,Date,window:{addEventListener:(event,handler)=>{events[event]=handler;},dispatchEvent(){}},
    CustomEvent:class{constructor(type){this.type=type;}},setInterval:fn=>polls.push(fn),
    setTimeout:(fn,delay)=>{const id=Symbol();timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id),
  });
  await flush();return {get,model,calls,control,events,polls,timers,document};
}

test('defaults cover every account and keep second precision without an account selector',async()=>{
  const {get,calls}=await fixture();await get('gold-configure').click();
  assert.equal(get('gold-start-time').value,'00:00:01');
  assert.equal(get('gold-repeat-count').value,5);
  assert.equal(get('gold-interval').value,1);
  assert.equal(calls.filter(call=>call.url==='/api/daily-gold/refresh').length,1);
  const html=fs.readFileSync(new URL('../templates/daily_gold.html',import.meta.url),'utf8');
  assert.match(html,/type="time" step="1"/);
  assert.doesNotMatch(html,/account_ids|选择账号|全选账号/);
  assert.doesNotMatch(texts(get('gold-accounts')),/今日已打卡/);
});

test('settings save numeric attempts and interval with no account binding',async()=>{
  const {get,calls}=await fixture();await get('gold-configure').click();
  get('gold-start-time').value='09:30';get('gold-repeat-count').value='8';get('gold-interval').value='2.5';
  await get('gold-settings-form').handlers.submit({preventDefault(){}});
  assert.deepEqual(calls.find(call=>call.url==='/api/daily-gold/settings').body,
    {enabled:true,start_time:'09:30:00',repeat_count:8,interval_seconds:2.5});
  assert.equal(get('gold-settings').open,false);
});

test('invalid settings stay editable and show the backend explanation',async()=>{
  const {get,control}=await fixture();await get('gold-configure').click();control.error='次数范围错误';
  get('gold-repeat-count').value='-1';await get('gold-settings-form').handlers.submit({preventDefault(){}});
  assert.equal(get('gold-settings').open,true);
  assert.equal(get('gold-settings-error').textContent,'次数范围错误');
  assert.equal(get('gold-repeat-count').value,'-1');
});

test('prizes show official names, state and receipt time as plain text',async()=>{
  const {get}=await fixture({accounts:[{id:'a',name:'0314',phone_masked:'182****0314',status:'active',
    observation:{date:'2031-04-15',signed:true,prizes:[{name:'2元话费券<img>',status:'已发放',received_at:'2031-04-15T00:00:02+08:00'}]}}]});
  const result=texts(get('gold-accounts'));
  assert.match(result,/2元话费券<img>.*已发放.*2031/);
  assert.match(result,/今日已打卡/);
  assert.equal(walk(get('gold-accounts'),'立即打卡').disabled,true);
  assert.equal(walk(get('gold-accounts'),'2元话费券<img>').innerHTML,undefined);
});

test('yesterday success and failed requests never become a false success today',async()=>{
  const {get,calls}=await fixture({refreshError:'官方暂未通过认证',accounts:[{id:'a',name:'0314',phone_masked:'182****0314',status:'active',
    observation:{date:'2031-04-14',signed:true},last_result:{state:'failed',message:'官方暂未通过认证'}}]});
  assert.doesNotMatch(texts(get('gold-accounts')),/今日已打卡/);
  assert.match(texts(get('gold-accounts')),/待查询.*奖品待查询/);
  assert.equal(get('gold-feedback').textContent,'官方暂未通过认证');
  assert.equal(calls.some(call=>call.url.includes('/sms')),false);
});

test('an ended campaign keeps readable history and disables all activity writes',async()=>{
  const {get,calls}=await fixture({ended:true});
  assert.equal(get('gold-ended').hidden,false);
  assert.equal(get('gold-configure').disabled,true);
  assert.equal(get('gold-settings-save').disabled,true);
  assert.equal(walk(get('gold-accounts'),'立即打卡').disabled,true);
  assert.equal(walk(get('gold-accounts'),'查询').disabled,true);
  assert.equal(walk(get('gold-accounts'),'记录').disabled,false);
  assert.equal(calls.some(call=>call.body),false);
});

test('automatic polling only reads local status and pauses when hidden',async()=>{
  const {calls,polls,document}=await fixture();const before=calls.length;
  await polls[0]();await flush();
  assert.deepEqual(calls.slice(before),[{url:'/api/daily-gold/status',body:undefined}]);
  document.hidden=true;const hiddenCount=calls.length;await polls[0]();await flush();
  assert.equal(calls.length,hiddenCount);
});

test('login notifications during refresh are coalesced and followed by one fresh official query',async()=>{
  let notified=false;
  const {calls}=await fixture({beforeResponse:({url,events})=>{
    if(url==='/api/daily-gold/refresh'&&!notified){notified=true;events['accounts-changed']();events['accounts-changed']();}
  }});
  assert.equal(calls.filter(call=>call.url==='/api/daily-gold/refresh').length,2);
});
