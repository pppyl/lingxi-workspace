import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.handlers={};this.value='';this.hidden=false;this.disabled=false;this.checked=false;this.attrs={};this.dataset={};this.classList={toggle(){}};}
  addEventListener(name,fn){this.handlers[name]=fn;}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;}
  setAttribute(key,value){this.attrs[key]=value;}
  reset(){}
  showModal(){this.open=true;}
  close(){this.open=false;}
  click(){if(!this.disabled)return this.handlers.click?.();}
}
const walk=(node,label)=>node.textContent===label?node:node.children.map(n=>walk(n,label)).find(Boolean);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
async function fixture(){
  const elements=new Map(),calls=[],timers=[],get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  get('star-filter').value='all';
  const accounts=[
    {id:'a',name:'未领取',phone_masked:'138****0001',status:'active',state:'ready',today_state:'signed',month_claimed:false,history:[]},
    {id:'b',name:'备用',phone_masked:'138****0002',status:'active',state:'ready',today_state:'signed',month_claimed:false,history:[]},
    {id:'c',name:'已领取',phone_masked:'138****0003',status:'active',state:'ready',today_state:'signed',month_claimed:true,benefit_id:'789',benefit_name:'官方领取月卡',history:[]},
    {id:'d',name:'待登录',phone_masked:'138****0004',status:'login_required',state:'login_required',today_state:'unknown',month_claimed:false,history:[]},
  ];
  const products=[{id:'123',phase:1,name:'视频月卡',stock:100,transferable:true},{id:'456',phase:2,name:'阅读月卡',stock:60,transferable:false}];
  const jobs=[{id:'future',product_name:'原预约',at:'2026-11-02T10:00:00+08:00',login_ids:['a'],offsets_ms:[-50,0,50],burst_count:3,results:{},state:'waiting',enabled:true}];
  const model={accounts,products:[],jobs,settings:{signin_enabled:true,signin_time:'00:01'},server_time:'2026-10-31T23:00:00+08:00',warning:''};
  const all=selector=>{
    const options=get('star-account-options').children.map(row=>row.children[0]);
    if(selector==='#star-account-options input:not(:disabled)')return options.filter(b=>!b.disabled);
    if(selector==='#star-account-options input:checked')return options.filter(b=>b.checked);
    if(selector==='#star-offsets input')return get('star-offsets').children.map(w=>w.children[0].children[0]);
    return [];
  };
  const fetch=async(url,options)=>{
    const body=options?.body?JSON.parse(options.body):undefined;calls.push({url,body});let result;
    if(url==='/api/star-day/status')result=model;
    else if(url==='/api/star-day/open')result={message:'前台检查已安排'};
    else if(url==='/api/star-day/login/sms')result={sent:true,retry_after:60,message:'验证码已发送'};
    else if(url==='/api/star-day/login/submit'){model.accounts.find(a=>a.id===body.login_id).state='ready';result={message:'短信登录成功'};}
    else if(url==='/api/star-day/products'){model.products=products;result={products};}
    else if(url==='/api/star-day/claim')result={id:'manual',message:'已安排立即领取'};
    else if(url==='/api/star-day/jobs')result={id:'new-future',message:'预约已保存'};
    else if(url==='/api/star-day/jobs/stop'){model.jobs=model.jobs.filter(j=>j.id!==body.id);result={message:'任务已停止并移除'};}
    else if(url==='/api/star-day/recheck')result={message:'重新认证已安排'};
    else if(url.startsWith('/api/star-day/gift/options?'))result={can_send_other:url.endsWith('=b'),family:[{id:'f'.repeat(64),name:'父亲',phone_masked:'138****0099'}]};
    else if(url==='/api/star-day/gift')result={message:'赠送已安排'};
    else throw new Error('Unexpected API '+url);
    return {ok:true,json:async()=>structuredClone(result)};
  };
  vm.runInNewContext(fs.readFileSync(new URL('../static/star-day.js',import.meta.url),'utf8'),{
    document:{getElementById:get,createElement:tag=>new Element(tag),querySelectorAll:all,querySelector:()=>null,hidden:false},
    window:{addEventListener(){}},fetch,Date,setInterval(fn,ms){timers.push({fn,ms});},
  });
  await flush();return {get,calls,all,accounts,jobs,timers};
}

test('unclaimed account opens immediate picker with only that account selected and no claim on opening',async()=>{
  const {get,calls,all}=await fixture();
  walk(get('star-accounts').children[0],'立即领取').click();await flush();
  assert.equal(get('star-reservation').open,true);
  assert.equal(get('star-reservation-title').textContent,'立即领取权益');
  assert.equal(get('star-reservation-timing').hidden,true);
  assert.equal(get('star-reservation-timing').disabled,true);
  assert.doesNotMatch(fs.readFileSync(new URL('../templates/star_day.html',import.meta.url),'utf8'),/id="star-at"/);
  assert.deepEqual(all('#star-account-options input:checked').map(b=>b.value),['a']);
  assert.equal(calls.filter(c=>c.url==='/api/star-day/products').length,1);
  assert.equal(calls.some(c=>c.body&&c.url!=='/api/star-day/open'),false);
  assert.equal(walk(get('star-accounts').children[2],'立即领取'),undefined);
});

test('immediate submission contains chosen benefit and selected account without timing or burst values',async()=>{
  const {get,calls}=await fixture();
  walk(get('star-accounts').children[0],'立即领取').click();await flush();
  get('star-claim-product').value='2:456';
  get('star-reservation-form').handlers.submit({preventDefault(){}});await flush();
  const sent=calls.find(c=>c.url==='/api/star-day/claim');
  assert.deepEqual(sent.body,{product_id:'456',phase:2,login_ids:['a']});
  assert.equal(get('star-reservation').open,false);
  assert.equal(calls.some(c=>c.url==='/api/star-day/jobs'),false);
});

test('select all for immediate claim excludes claimed and expired accounts',async()=>{
  const {get,calls,all}=await fixture();
  walk(get('star-accounts').children[0],'立即领取').click();await flush();
  get('star-select-all').checked=true;get('star-select-all').handlers.change();
  assert.deepEqual(all('#star-account-options input:checked').map(b=>b.value),['a','b']);
  get('star-reservation-form').handlers.submit({preventDefault(){}});await flush();
  assert.deepEqual(calls.find(c=>c.url==='/api/star-day/claim').body.login_ids,['a','b']);
});

test('product row supports immediate batch claim even when displayed stock is sold out',async()=>{
  const {get,calls,all}=await fixture();
  get('star-products-refresh').click();await flush();
  walk(get('star-products').children[0],'立即领取').click();await flush();
  assert.equal(get('star-claim-product').value,'1:123');
  assert.deepEqual(all('#star-account-options input:checked').map(b=>b.value),['a','b']);
  assert.equal(get('star-reservation').open,true);
  assert.equal(calls.some(c=>c.url==='/api/star-day/calibrate'),false);
});

test('reservation after immediate mode restores timing controls and original offset workflow',async()=>{
  const {get,calls}=await fixture();
  walk(get('star-accounts').children[0],'立即领取').click();await flush();
  get('star-reservation').close();walk(get('star-products').children[0],'预约抢购').click();
  assert.equal(get('star-reservation-title').textContent,'配置每月权益领取');
  assert.equal(get('star-reservation-timing').hidden,false);assert.equal(get('star-reservation-timing').disabled,false);
  assert.equal(get('star-product-choice').hidden,true);
  get('star-reservation-form').handlers.submit({preventDefault(){}});await flush();
  const body=calls.find(c=>c.url==='/api/star-day/jobs').body;
  assert.deepEqual(body.offsets_ms,[-50,0,50,100,150]);assert.equal(body.at,undefined);
  assert.equal(calls.some(c=>c.url==='/api/star-day/claim'),false);
});

test('monthly unclaimed filter makes remaining accounts easy to locate',async()=>{
  const {get}=await fixture();get('star-filter').value='unclaimed';get('star-filter').handlers.change();
  assert.equal(get('star-accounts').children.length,3);
  assert.equal(walk(get('star-accounts'),'已领取'),undefined);
});

test('monthly benefit column displays resolved receipt name and explicitly labels unresolved names',async()=>{
  const {get,accounts}=await fixture();
  assert.ok(walk(get('star-accounts').children[2],'官方领取月卡'));
  accounts[2].benefit_name='';get('star-refresh').click();await flush();
  assert.ok(walk(get('star-accounts').children[2],'权益 789 · 名称待查询'));
});


test('stop removes the task from the list rather than retaining a stopped row',async()=>{
  const {get,calls}=await fixture();walk(get('star-jobs'),'停止并移除').click();await flush();
  assert.equal(calls.filter(c=>c.url==='/api/star-day/jobs/stop').length,1);
  assert.equal(walk(get('star-jobs'),'停止并移除'),undefined);
  assert.equal(walk(get('star-jobs'),'原预约 · 1 个账号'),undefined);
});

test('SMS login stays disabled outside campaign authentication failure and sends no SMS on click',async()=>{
  const {get,calls,accounts}=await fixture();
  for(const state of ['ready','waiting_session','ineligible','failed','running','login_required','rate_limited']){
    accounts[0].state=state;
    get('star-refresh').click();await flush();
    const sms=walk(get('star-accounts').children[0],'短信登录');
    assert.equal(sms.disabled,true,state);
    sms.click();await flush();
  }
  assert.equal(get('star-sms').open,undefined);
  assert.equal(calls.some(c=>c.url==='/api/star-day/login/sms'),false);
  assert.equal(walk(get('star-accounts').children[3],'短信登录'),undefined);
});

test('campaign authentication failure offers one SMS recovery instead of repeating APP SSO',async()=>{
  const {get,calls,accounts}=await fixture();accounts[0].state='auth_failed';accounts[0].today_state='ineligible';
  get('star-refresh').click();await flush();
  const row=get('star-accounts').children[0];assert.ok(walk(row,'活动认证待恢复'));
  assert.equal(walk(row,'不符合资格'),undefined);
  assert.equal(calls.some(c=>c.url==='/api/star-day/login/sms'),false);
  walk(row,'短信登录').click();await flush();
  assert.deepEqual(calls.find(c=>c.url==='/api/star-day/login/sms').body,{login_id:'a'});
  assert.equal(get('star-sms').open,true);
  assert.equal(calls.some(c=>c.url==='/api/star-day/recheck'),false);
  assert.equal(calls.some(c=>c.url==='/api/star-day/claim'),false);
});

test('rate limiting keeps manual benefit claim available without treating the account as logged out',async()=>{
  const {get,calls,accounts,all}=await fixture();
  Object.assign(accounts[0],{state:'rate_limited',today_state:'signed',retry_reason:'rate_limited',retry_at:'2026-11-02T09:30:00+08:00'});
  get('star-refresh').click();await flush();
  const row=get('star-accounts').children[0];
  assert.ok(walk(row,'今日已签到'));
  assert.ok(walk(row,'限流后暂停自动重试'));
  assert.equal(walk(row,'短信登录').disabled,true);
  assert.equal(walk(row,'立即领取').disabled,false);
  assert.equal(walk(row,'赠送权益').disabled,false);
  walk(row,'立即领取').click();await flush();
  assert.equal(get('star-reservation').open,true);
  assert.deepEqual(all('#star-account-options input:checked').map(b=>b.value),['a']);
  assert.equal(calls.some(c=>c.url==='/api/star-day/login/sms'||c.url==='/api/star-day/recheck'||c.url==='/api/star-day/claim'),false);
});

test('later authentication failure does not erase an officially confirmed sign-in for today',async()=>{
  const {get,accounts}=await fixture();
  Object.assign(accounts[0],{state:'auth_failed',today_state:'signed',message:'活动认证未通过'});
  get('star-refresh').click();await flush();
  const row=get('star-accounts').children[0];
  assert.ok(walk(row,'今日已签到'));
  assert.equal(walk(row,'活动认证待恢复'),undefined);
  assert.equal(walk(row,'短信登录').disabled,false);
});

test('daily eligibility and authentication checks display the next date instead of continuous recovery',async()=>{
  const {get,calls,accounts}=await fixture();
  Object.assign(accounts[0],{state:'ineligible',today_state:'ineligible',retry_reason:'ineligible',retry_at:'2026-11-02T00:01:00+08:00'});
  Object.assign(accounts[1],{state:'auth_failed',retry_reason:'auth_failed',retry_at:'2026-11-02T00:01:00+08:00'});
  get('star-refresh').click();await flush();
  assert.ok(walk(get('star-accounts').children[0],'资格每日检查一次'));
  assert.ok(walk(get('star-accounts').children[1],'认证每日检查一次'));
  assert.equal(walk(get('star-accounts'),'活动认证恢复后自动检查'),undefined);
  walk(get('star-accounts').children[0],'重新检查').click();await flush();
  assert.deepEqual(calls.find(c=>c.url==='/api/star-day/recheck').body,{login_ids:['a']});
});

test('expired primary login is shown separately from campaign eligibility cooldown',async()=>{
  const {get,accounts}=await fixture();
  Object.assign(accounts[3],{retry_reason:'ineligible',retry_at:'2026-11-02T00:01:00+08:00'});
  get('star-refresh').click();await flush();
  const row=get('star-accounts').children[3];
  assert.ok(walk(row,'重新登录后检查'));
  assert.ok(walk(row,'重新登录'));
  assert.equal(walk(row,'重新检查'),undefined);
});


test('opening a gift reads verified recipients but does not submit until the recipient is confirmed',async()=>{
  const {get,calls}=await fixture();walk(get('star-accounts').children[0],'赠送权益').click();await flush();
  assert.equal(get('star-gift').open,true);assert.equal(get('star-gift-source').value,'a');
  assert.equal(get('star-gift-product').children.length,1);
  assert.equal(get('star-gift-submit').disabled,true);
  get('star-gift-family').value='f'.repeat(64);get('star-gift-family').handlers.change();
  assert.equal(get('star-gift-submit').disabled,false);
  assert.match(get('star-gift-summary').textContent,/未领取.*父亲/);
  assert.equal(calls.some(c=>c.url==='/api/star-day/gift'),false);
  get('star-gift-form').handlers.submit({preventDefault(){}});await flush();
  assert.deepEqual(calls.find(c=>c.url==='/api/star-day/gift').body,{product_id:'123',phase:1,login_ids:['a'],recipient:{kind:'family',id:'f'.repeat(64)},mode:'now'});
});

test('daily rush gift confirms donor and recipient in advance and preserves the chosen offsets',async()=>{
  const {get,calls}=await fixture();walk(get('star-accounts').children[0],'赠送权益').click();await flush();
  get('star-gift-family').value='f'.repeat(64);get('star-gift-mode').value='daily';get('star-gift-mode').handlers.change();
  get('star-gift-offsets').value='-80,0,45,140,220';get('star-gift-offsets').handlers.input();
  assert.equal(get('star-gift-timing').hidden,false);assert.equal(get('star-gift-submit').disabled,false);
  assert.match(get('star-gift-summary').textContent,/每天 10:00/);
  get('star-gift-form').handlers.submit({preventDefault(){}});await flush();
  const body=calls.find(c=>c.url==='/api/star-day/gift').body;
  assert.equal(body.mode,'daily');assert.deepEqual(body.offsets_ms,[-80,0,45,140,220]);assert.deepEqual(body.login_ids,['a']);
  assert.equal(body.recipient.id,'f'.repeat(64));assert.equal(body.at,undefined);
});

test('ordinary donor cannot choose arbitrary phone and a changed donor reloads official recipient permissions',async()=>{
  const {get}=await fixture();walk(get('star-accounts').children[0],'赠送权益').click();await flush();
  assert.equal(get('star-gift-kind').children.length,1);
  get('star-gift-source').value='b';get('star-gift-source').handlers.change();await flush();
  assert.equal(get('star-gift-kind').children.length,2);assert.equal(get('star-gift-family').value,'');
  get('star-gift-kind').value='phone';get('star-gift-kind').handlers.change();
  assert.equal(get('star-gift-submit').disabled,true);
  get('star-gift-phone').value='13800000099';get('star-gift-phone').handlers.input();
  assert.equal(get('star-gift-submit').disabled,false);
});

test('automatic refresh only reads local status and never reopens login or sends SMS',async()=>{
  const {calls,timers}=await fixture();
  const poll=timers.find(t=>t.ms===5000);
  for(let i=0;i<6;i++){await poll.fn();await flush();}
  assert.equal(calls.filter(c=>c.url==='/api/star-day/open').length,1);
  assert.equal(calls.some(c=>c.url==='/api/star-day/login/sms'),false);
  assert.equal(calls.some(c=>c.url==='/api/star-day/recheck'),false);
});

test('SMS recovery submits entered code to the selected account without a primary APP login',async()=>{
  const {get,calls,accounts}=await fixture();
  accounts[0].state='auth_failed';
  get('star-refresh').click();await flush();
  walk(get('star-accounts').children[0],'短信登录').click();await flush();
  assert.equal(get('star-sms-send').disabled,true);
  get('star-sms-code').value='012345';
  get('star-sms-form').handlers.submit({preventDefault(){}});await flush();
  assert.deepEqual(calls.find(c=>c.url==='/api/star-day/login/submit').body,{login_id:'a',code:'012345'});
  assert.equal(get('star-sms').open,false);
  assert.equal(calls.filter(c=>c.url==='/api/star-day/login/sms').length,1);
  assert.equal(walk(get('star-accounts').children[0],'短信登录').disabled,true);
});
