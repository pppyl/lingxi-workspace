import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(){this.children=[];this.handlers={};this.value='';this.hidden=false;this.disabled=false;this.checked=false;this.dataset={};this.classList={toggle(){}};}
  addEventListener(name,fn){this.handlers[name]=fn;}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;}
  setAttribute(){}
  reset(){}
  showModal(){this.open=true;}
  close(){this.open=false;}
  click(){if(!this.disabled)return this.handlers.click?.();}
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const walk=(node,label)=>node.textContent===label?node:node.children.map(child=>walk(child,label)).find(Boolean);
async function fixture(){
  const elements=new Map(),calls=[],get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  get('monthly-filter').value='all';
  const model={accounts:[],automation:{enabled:true,time:'08:05',retry_interval_minutes:5,max_retries:5},page:{
    entry_url:'https://wap.gz.10086.cn/activityWeb/static/activity/grc/index.html?referChannels=current',
    default_entry_url:'https://wap.gz.10086.cn/activityWeb/static/activity/grc/index.html?referChannels=default',
  },server_time:'2026-11-01T08:00:00+08:00',warning:''};
  const control={pageError:'',settingsError:''};
  const fetch=async(url,options)=>{
    const body=options?.body?JSON.parse(options.body):undefined;calls.push({url,body});
    if(url==='/api/monthly-coupon/status')return {ok:true,json:async()=>structuredClone(model)};
    if(url==='/api/monthly-coupon/page'){
      if(control.pageError)return {ok:false,json:async()=>({error:control.pageError})};
      model.page.entry_url=body.entry_url;
      return {ok:true,json:async()=>({page:structuredClone(model.page),message:'活动地址已保存'})};
    }
    if(url==='/api/monthly-coupon/settings'){
      if(control.settingsError)return {ok:false,json:async()=>({error:control.settingsError})};
      if(!body.login_ids)model.automation=structuredClone(body);
      return {ok:true,json:async()=>({message:'自动设置已保存'})};
    }
    throw new Error('Unexpected API '+url);
  };
  vm.runInNewContext(fs.readFileSync(new URL('../static/monthly_coupon.js',import.meta.url),'utf8'),{
    document:{getElementById:get,createElement:()=>new Element(),querySelectorAll:()=>[],hidden:false},
    window:{addEventListener(){}},fetch,Date,setInterval(){},
  });
  await flush();return {get,calls,model,control};
}

test('activity address dialog uses the server URL and opening it performs no account action',async()=>{
  const {get,calls,model}=await fixture();
  get('monthly-page-configure').click();
  assert.equal(get('monthly-page-settings').open,true);
  assert.equal(get('monthly-entry-url').value,model.page.entry_url);
  assert.equal(calls.some(call=>call.body),false);
});

test('saving an activity address updates only the page setting and refreshes displayed state',async()=>{
  const {get,calls,model}=await fixture();
  const address='https://wap.gz.10086.cn/activityWeb/static/activity/grc/index.html?referChannels=updated&actId=123';
  get('monthly-page-configure').click();get('monthly-entry-url').value=address;
  get('monthly-page-form').handlers.submit({preventDefault(){}});await flush();
  assert.deepEqual(calls.filter(call=>call.body),[{url:'/api/monthly-coupon/page',body:{entry_url:address}}]);
  assert.equal(model.page.entry_url,address);
  assert.equal(get('monthly-page-settings').open,false);
  assert.equal(get('monthly-feedback').textContent,'活动地址已保存');
  get('monthly-page-configure').click();assert.equal(get('monthly-entry-url').value,address);
});

test('backend validation errors remain in the address dialog without claiming a successful save',async()=>{
  const {get,model,control}=await fixture();const original=model.page.entry_url;
  control.pageError='请选择贵州移动官方活动入口';
  get('monthly-page-configure').click();get('monthly-entry-url').value='https://example.com/';
  get('monthly-page-form').handlers.submit({preventDefault(){}});await flush();
  assert.equal(get('monthly-page-settings').open,true);
  assert.equal(get('monthly-page-error').textContent,control.pageError);
  assert.equal(get('monthly-page-error').hidden,false);
  assert.equal(model.page.entry_url,original);
  assert.equal(get('monthly-page-save').disabled,false);
});

test('using the supplied default address only edits the field until the user saves',async()=>{
  const {get,calls,model}=await fixture();const original=model.page.entry_url;
  get('monthly-page-configure').click();get('monthly-page-default').click();
  assert.equal(get('monthly-entry-url').value,model.page.default_entry_url);
  assert.equal(model.page.entry_url,original);
  assert.equal(calls.some(call=>call.body),false);
});

test('automatic settings load the saved retry policy including zero retries',async()=>{
  const {get,model}=await fixture();
  Object.assign(model.automation,{retry_interval_minutes:17,max_retries:0});
  get('monthly-refresh').click();await flush();get('monthly-configure').click();
  assert.equal(get('monthly-retry-interval').value,17);
  assert.equal(get('monthly-max-retries').value,0);
  assert.match(get('monthly-retry-note').textContent,/最多再重试 0 次，间隔 17 分钟/);
  assert.match(get('monthly-retry-note').textContent,/不符合活动资格的账号仍每天检查一次/);
});

test('automatic settings save retry fields as numbers with the enabled flag and time',async()=>{
  const {get,calls}=await fixture();get('monthly-configure').click();
  get('monthly-auto-time').value='09:35';
  get('monthly-retry-interval').value='2';get('monthly-max-retries').value='0';
  get('monthly-max-retries').handlers.input();
  assert.match(get('monthly-retry-note').textContent,/最多再重试 0 次，间隔 2 分钟/);
  get('monthly-settings-form').handlers.submit({preventDefault(){}});await flush();
  assert.deepEqual(calls.find(call=>call.url==='/api/monthly-coupon/settings').body,
    {enabled:true,time:'09:35',retry_interval_minutes:2,max_retries:0});
  assert.equal(get('monthly-settings').open,false);
});

test('disabling one account preserves the current global retry policy',async()=>{
  const {get,calls,model}=await fixture();
  Object.assign(model.automation,{retry_interval_minutes:23,max_retries:8});
  model.accounts=[{id:'account-a',name:'测试账号',phone_masked:'182****0001',status:'active',eligible:true,
    auto_enabled:true,direct_state:'available',bonus_state:'available',tasks:[]}];
  get('monthly-refresh').click();await flush();
  walk(get('monthly-accounts'),'停用自动').click();await flush();
  assert.deepEqual(calls.find(call=>call.url==='/api/monthly-coupon/settings').body,
    {enabled:false,time:'08:05',retry_interval_minutes:23,max_retries:8,login_ids:['account-a']});
});

test('retry settings validation error stays in the dialog and retains entered values',async()=>{
  const {get,control}=await fixture();control.settingsError='重试次数必须为 0 至 100 的整数';
  get('monthly-configure').click();get('monthly-max-retries').value='101';
  get('monthly-settings-form').handlers.submit({preventDefault(){}});await flush();
  assert.equal(get('monthly-settings').open,true);
  assert.equal(get('monthly-settings-error').textContent,control.settingsError);
  assert.equal(get('monthly-settings-error').hidden,false);
  assert.equal(get('monthly-max-retries').value,'101');
  assert.equal(get('monthly-settings-save').disabled,false);
});

test('account retry progress only appears after a retry or when the daily budget is exhausted',async()=>{
  const {get,model}=await fixture();
  const account={id:'account-a',name:'测试账号',phone_masked:'182****0001',status:'active',eligible:true,
    auto_enabled:true,direct_state:'available',bonus_state:'available',tasks:[],retry_count:0,max_retries:5,retry_exhausted:false};
  model.accounts=[account];get('monthly-refresh').click();await flush();
  assert.equal(walk(get('monthly-accounts'),'失败重试 0 / 5 次'),undefined);
  account.retry_count=2;get('monthly-refresh').click();await flush();
  assert.ok(walk(get('monthly-accounts'),'失败重试 2 / 5 次'));
  account.retry_count=5;account.retry_exhausted=true;get('monthly-refresh').click();await flush();
  assert.ok(walk(get('monthly-accounts'),'今日重试已用完'));
  assert.equal(walk(get('monthly-accounts'),'失败重试 2 / 5 次'),undefined);
});

test('completed daily work shows completion and pending bonus rather than an unchecked reward',async()=>{
  const {get,model}=await fixture();
  model.accounts=[{id:'account-a',name:'0314',phone_masked:'182****0314',status:'active',eligible:true,
    auto_enabled:true,direct_state:'claimed',bonus_state:'pending',tasks:[],daily_completed:true,
    retry_count:0,max_retries:5,retry_exhausted:false,punch_days:1,punched_today:true}];
  get('monthly-refresh').click();await flush();
  assert.ok(walk(get('monthly-accounts'),'今日任务已完成'));
  assert.ok(walk(get('monthly-accounts'),'待完成任务'));
  assert.equal(walk(get('monthly-accounts'),'未查询'),undefined);
  assert.equal(walk(get('monthly-accounts'),'失败重试 1 / 5 次'),undefined);
});

test('monthly limit without a prize is shown as unverified and appears under attention',async()=>{
  const {get,model}=await fixture();
  model.accounts=[{id:'account-a',name:'0314',phone_masked:'182****0314',status:'active',eligible:true,
    direct_state:'participated',direct_message:'官方提示本月参与次数已用完（901），奖品名称与发券结果待核实',
    bonus_state:'pending',tasks:[],auto_enabled:true,daily_completed:true}];
  get('monthly-refresh').click();await flush();
  assert.ok(walk(get('monthly-accounts'),'奖品待核实'));
  assert.equal(walk(get('monthly-accounts'),'本月已领取'),undefined);
  get('monthly-filter').value='attention';get('monthly-filter').handlers.change();
  assert.ok(walk(get('monthly-accounts'),'0314'));
});

test('a verified used coupon shows its name and use time instead of a generic claimed badge',async()=>{
  const {get,model}=await fixture();
  model.accounts=[{id:'account-a',name:'0314',phone_masked:'182****0314',status:'active',eligible:true,
    direct_state:'claimed',direct_prize:'4元话费满减券',direct_message:'官方卡券历史已确认领取并使用',
    direct_receipt:{source:'wallet_history',status:'used',used_at:'2026-11-01T01:30:09+08:00'},
    bonus_state:'pending',tasks:[],auto_enabled:true,daily_completed:true}];
  get('monthly-refresh').click();await flush();
  assert.ok(walk(get('monthly-accounts'),'已使用'));
  assert.ok(walk(get('monthly-accounts'),'4元话费满减券'));
  assert.ok(walk(get('monthly-accounts'),'使用于 2026/11/01 01:30:09'));
  assert.equal(walk(get('monthly-accounts'),'本月已领取'),undefined);
});
