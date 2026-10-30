import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag='div') { this.tag=tag;this.children=[];this.handlers={};this.value='';this.hidden=true;this.open=false;this.contentWindow={};this.classList={add(){},remove(){},toggle(){}}; }
  addEventListener(type,handler) { this.handlers[type]=handler; }
  setAttribute() {}
  removeAttribute() {}
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; }
  querySelector(tag) { return this.children.find(node=>node.tag===tag)||null; }
  reset() {}
  focus() {}
  showModal() { this.open=true; }
  close() { this.open=false; }
  requestSubmit() { return this.handlers.submit({preventDefault(){}}); }
  click() { return this.handlers.click(); }
}

async function fixture(mode,options={}) {
  const elements=new Map(),calls=[],events={},timers=new Map(),get=id=>{
    if(!elements.has(id))elements.set(id,new Element());return elements.get(id);
  };
  get('coupons-use-verification').append(new Element('p'));
  get('coupons-category').value='all';
  const pending=mode==='payment'?[{order_id:'existing-order',coupon_name:'fixture',amount:'30'}]:[];
  const card={id:'coupon',name:'fixture',use_kind:['exchange','rights'].includes(mode)?mode:'recharge',category:mode==='rights'?'flow':'fee',status:'available'};
  let loggedIn=false,loginAttempts=0,prepareCount=0,accepted=false;
  const http=async(url,fetchOptions)=>{
    const body=fetchOptions?.body?JSON.parse(fetchOptions.body):undefined;calls.push({url,body});
    let result,status=200;
    if(url==='/api/coupons/accounts')result={accounts:options.accounts||[{id:'account',status:'active',name:'fixture',phone_masked:'138****0000'}]};
    else if(url.startsWith('/api/coupons/pending?'))result={pending_orders:pending};
    else if(url.startsWith('/api/coupons?')){
      const accountId=new URLSearchParams(url.split('?')[1]).get('login_id');
      if(options.countFailureIds?.includes(accountId)){status=401;result={code:'APP_LOGIN_REQUIRED',error:'账号需要重新登录'};}
      else{
        const rows=accepted&&options.consumeOnAccepted?[]:options.itemsByAccount?.[accountId]||[card];
        result={items:rows,page:1,total:rows.length,has_more:false,pending_orders:pending};
      }
    }
    else if(url==='/api/logins/relogin/sms')result={sent:true,phone_masked:'138****0000',retry_after:60};
    else if(url==='/api/logins/relogin'){
      loginAttempts++;
      if(options.wrongCode&&loginAttempts===1){status=400;result={error:'验证码错误'};}
      else {loggedIn=true;result={account:{id:'account',status:'active',name:'fixture',phone_masked:'138****0000'}};}
    }
    else if(url==='/api/coupons/use/prepare'){
      if(options.prepareAuth&&!loggedIn){status=401;result={code:'APP_LOGIN_REQUIRED',error:'请重新登录'};}
      else result={operation_id:++prepareCount===1?'operation':'operation-'+prepareCount,kind:card.use_kind,choices:[{value:'30',label:'充值30元'}]};
    }
    else if(url==='/api/coupons/use/confirm'||url==='/api/coupons/use/payment') {
      if(options.failedResultCode&&(!loggedIn||options.repeatError)){
        result={state:'failed',code:options.failedResultCode,message:'官方检测到充值环境异常，本次未生成支付订单。'};
      }else if(options.errorCode&&(!loggedIn||options.repeatError)){
        status=409;result={code:options.errorCode,error:'需要验证'};
      }else if(['exchange','rights'].includes(mode))result=body.prepare_risk?{state:'risk_required',sdk_server:'/coupon-verification/wallet/udid/c1?operation_id=operation'}:{state:'accepted',message:'兑换申请已受理'};
      else result=(loggedIn||body.captcha_token||(url==='/api/coupons/use/payment'&&mode==='submission'))?{state:'payment_required',order_id:'existing-order',amount:'30.00',payment_url:'https://mclient.alipay.com/cashierRoutePay.htm?fixture',qr_image:'fixture-qr'}:
        {state:'verification_required',verification:'captcha',message:'请人工验证',challenge:{app_id:'provider-app',risk_id:'provider-risk'}};
    } else throw new Error('Unexpected API '+url);
    if(result.state==='accepted')accepted=true;
    return {ok:status===200,status,json:async()=>result};
  };
  vm.runInNewContext(fs.readFileSync(new URL('../static/coupons.js',import.meta.url),'utf8'),{
    document:{cookie:'session=private; _zw_kvani5r=real-sdk-lid',getElementById:get,createElement:tag=>new Element(tag),querySelectorAll:()=>[],head:{append(script){script.onload();}}},
    window:{addEventListener:(name,fn)=>{events[name]=fn;},publishAccountsChanged:()=>events['accounts-changed'](),
      _zw:{ConstID(config,done){calls.push({url:'official-sdk',body:config});done(options.sdkError||null,options.sdkError?'':'real-sdk-token');}}},fetch:http,location:{origin:'http://localhost:5500'},Intl,URLSearchParams,Date,
    clearInterval(){},setInterval(){return 1;},clearTimeout(id){timers.delete(id);},
    setTimeout(callback,delay){const id=Symbol();timers.set(id,{callback,delay});return id;},
  });
  await new Promise(resolve=>setImmediate(resolve));
  if(options.onlyCounts)return {get,calls,events,timers};
  const walk=(node,label)=>node.textContent===label?node:node.children.map(child=>walk(child,label)).find(Boolean);
  let submission;
  if(mode==='payment')await walk(get('coupons-pending-list'),'继续支付').click();
  else {
    await walk(get('coupons-list'),mode==='exchange'?'兑换话费':mode==='rights'?'兑换流量':'充值使用').click();
    submission=get('coupons-use-form').requestSubmit();if(!['exchange','rights'].includes(mode))await submission;
  }
  return {get,calls,events,submission,timers};
}

for(const mode of ['payment','submission'])test(`human verification automatically resumes ${mode} exactly once`,async()=>{
  const env=await fixture(mode),frame=env.get('coupons-use-captcha').querySelector('iframe');
  assert.ok(frame);
  const event={source:frame.contentWindow,origin:'http://localhost:5500',data:{type:'coupon-captcha-success',token:'human-token'}};
  const endpoint=mode==='payment'?'/api/coupons/use/payment':'/api/coupons/use/confirm';
  env.events.message({...event,origin:'https://unrelated.invalid'});
  assert.equal(env.calls.filter(call=>call.url===endpoint).length,1);
  env.events.message(event);env.events.message(event);
  await new Promise(resolve=>setImmediate(resolve));
  const resumed=env.calls.filter(call=>call.url===endpoint);
  assert.equal(resumed.length,2);
  assert.equal(resumed[1].body.captcha_token,'human-token');
  assert.equal(env.get('coupons-use-captcha').children.length,0);
  if(mode==='payment'){
    assert.equal(resumed[1].body.order_id,'existing-order');
    assert.equal(env.calls.some(call=>call.url==='/api/coupons/use/confirm'),false);
  }
  assert.equal(env.get('coupons-payment-url').value,'https://mclient.alipay.com/cashierRoutePay.htm?fixture');
});

for(const code of ['ALIPAY_RELOGIN_REQUIRED','APP_LOGIN_REQUIRED','COUPON_LOGIN_REQUIRED','500003'])test(`${code}: inline login sends one SMS and resumes original order`,async()=>{
  const env=await fixture('payment',{errorCode:code}),{get,calls}=env;
  assert.equal(get('coupons-use').open,true);
  assert.equal(get('coupons-use-relogin').hidden,false);
  const sms=calls.filter(call=>call.url==='/api/logins/relogin/sms');
  assert.equal(sms.length,1);
  assert.equal(sms[0].body.id,'account');assert.equal(sms[0].body.force,true);
  await get('coupons-relogin-sms').click();
  assert.equal(calls.filter(call=>call.url==='/api/logins/relogin/sms').length,1,'cooldown prevents a second SMS');
  get('coupons-relogin-code').value='123456';
  const login=get('coupons-relogin-confirm').click();
  await get('coupons-relogin-confirm').click();await login;
  assert.equal(calls.filter(call=>call.url==='/api/logins/relogin').length,1);
  const payments=calls.filter(call=>call.url==='/api/coupons/use/payment');
  assert.equal(payments.length,2);assert.equal(payments[1].body.order_id,'existing-order');
  assert.equal(payments[1].body.refresh,true);assert.equal(payments[1].body.captcha_token,'');
  assert.equal(calls.some(call=>call.url==='/api/coupons/use/confirm'),false,'never creates another order');
  assert.equal(get('coupons-use').open,true);
  assert.equal(get('coupons-use-relogin').hidden,true);
  assert.match(get('coupons-payment-url').value,/^https:\/\/mclient.alipay.com\//);
  assert.equal(get('coupons-relogin-code').value,'');
});

test('wrong login OTP keeps current popup and allows correction without another SMS',async()=>{
  const {get,calls}=await fixture('payment',{errorCode:'ALIPAY_RELOGIN_REQUIRED',wrongCode:true});
  get('coupons-relogin-code').value='123456';await get('coupons-relogin-confirm').click();
  assert.equal(get('coupons-relogin-error').textContent,'验证码错误');
  assert.equal(get('coupons-use-relogin').hidden,false);
  assert.equal(calls.filter(call=>call.url==='/api/coupons/use/payment').length,1);
  get('coupons-relogin-code').value='654321';await get('coupons-relogin-confirm').click();
  assert.equal(calls.filter(call=>call.url==='/api/logins/relogin/sms').length,1);
  assert.equal(get('coupons-use-relogin').hidden,true);
});

test('CV3 after successful login offers manual retry without an SMS or login loop',async()=>{
  const {get,calls}=await fixture('payment',{errorCode:'ALIPAY_RELOGIN_REQUIRED',repeatError:true});
  get('coupons-relogin-code').value='123456';await get('coupons-relogin-confirm').click();
  assert.equal(get('coupons-use-relogin').hidden,false);
  assert.match(get('coupons-relogin-message').textContent,/不会自动重复/);
  assert.equal(calls.filter(call=>call.url==='/api/logins/relogin/sms').length,1);
  assert.equal(calls.filter(call=>call.url==='/api/logins/relogin').length,1);
  assert.equal(calls.filter(call=>call.url==='/api/coupons/use/payment').length,2);
});

test('500002 remains a submission error and does not trigger login SMS',async()=>{
  const {get,calls}=await fixture('payment',{errorCode:'500002'});
  assert.equal(get('coupons-use-error').hidden,false);
  assert.equal(get('coupons-use-relogin').hidden,true);
  assert.equal(calls.some(call=>call.url.startsWith('/api/logins/')),false);
});

test('531022 in a successful HTTP response opens inline login and prepares a fresh operation after login',async()=>{
  const {get,calls}=await fixture('submission',{failedResultCode:'531022'});
  assert.equal(get('coupons-use').open,true);
  assert.equal(get('coupons-use-relogin').hidden,false);
  assert.equal(get('coupons-use-confirm').hidden,true);
  const sms=calls.filter(call=>call.url==='/api/logins/relogin/sms');
  assert.equal(sms.length,1);assert.equal(sms[0].body.id,'account');
  assert.equal(calls.filter(call=>call.url==='/api/coupons/use/confirm').length,1);
  get('coupons-relogin-code').value='123456';await get('coupons-relogin-confirm').click();
  assert.equal(get('coupons-use-relogin').hidden,true);
  assert.equal(get('coupons-use-confirm').disabled,false);
  assert.equal(calls.filter(call=>call.url==='/api/coupons/use/prepare').length,2);
  assert.equal(calls.filter(call=>call.url==='/api/coupons/use/confirm').length,1,'login does not submit an order');
  await get('coupons-use-form').requestSubmit();
  const confirms=calls.filter(call=>call.url==='/api/coupons/use/confirm');
  assert.deepEqual(confirms.map(call=>call.body.operation_id),['operation','operation-2']);
  assert.match(get('coupons-payment-url').value,/^https:\/\/mclient.alipay.com\//);
});

test('531022 recurring after login does not loop login SMS or automatically resubmit',async()=>{
  const {get,calls}=await fixture('submission',{failedResultCode:'531022',repeatError:true});
  get('coupons-relogin-code').value='123456';await get('coupons-relogin-confirm').click();
  await get('coupons-use-form').requestSubmit();
  assert.equal(get('coupons-use-relogin').hidden,false);
  assert.match(get('coupons-relogin-message').textContent,/不会自动重复/);
  assert.equal(calls.filter(call=>call.url==='/api/logins/relogin/sms').length,1);
  assert.equal(calls.filter(call=>call.url==='/api/logins/relogin').length,1);
  assert.equal(calls.filter(call=>call.url==='/api/coupons/use/confirm').length,2);
});

test('other rejected recharge results do not trigger login SMS',async()=>{
  const {get,calls}=await fixture('submission',{failedResultCode:'531015'});
  assert.equal(get('coupons-use-relogin').hidden,true);
  assert.equal(calls.some(call=>call.url.startsWith('/api/logins/')),false);
});

test('expired account during preparation logs in inline and requires confirmation before ordering',async()=>{
  const {get,calls}=await fixture('submission',{prepareAuth:true});
  assert.equal(get('coupons-use-relogin').hidden,false);
  get('coupons-relogin-code').value='123456';await get('coupons-relogin-confirm').click();
  assert.equal(calls.filter(call=>call.url==='/api/coupons/use/prepare').length,2);
  assert.equal(calls.some(call=>call.url==='/api/coupons/use/confirm'),false);
  assert.equal(get('coupons-use-confirm').disabled,false);
  assert.equal(get('coupons-use').open,true);
});

test('exchange validates SMS, then initializes official SDK, then submits its opaque result',async()=>{
  const {calls,submission}=await fixture('exchange');await submission;
  const confirms=calls.filter(call=>call.url==='/api/coupons/use/confirm');
  assert.equal(confirms.length,2);assert.equal(confirms[0].body.prepare_risk,true);
  assert.equal(confirms[1].body.risk_token,'real-sdk-token');
  assert.equal(confirms[1].body.risk_lid,'real-sdk-lid');
  assert.equal(JSON.stringify(confirms[1].body).includes('private'),false);
  const sdkIndex=calls.findIndex(call=>call.url==='official-sdk');
  assert.ok(calls.indexOf(confirms[0])<sdkIndex&&sdkIndex<calls.indexOf(confirms[1]));
  assert.equal(calls[sdkIndex].body.server,'http://localhost:5500/coupon-verification/wallet/udid/c1?operation_id=operation');
  assert.equal(calls[sdkIndex].body.cache,false);
});

test('official SDK initialization failure keeps verified SMS and allows retry without submitting exchange',async()=>{
  const {get,calls,submission}=await fixture('exchange',{sdkError:true});await submission;
  const confirms=calls.filter(call=>call.url==='/api/coupons/use/confirm');
  assert.equal(confirms.length,1);assert.equal(confirms[0].body.prepare_risk,true);
  assert.equal(get('coupons-use-confirm').disabled,false);
  assert.match(get('coupons-use-error').textContent,/官方用券校验未完成/);
});

for(const mode of ['exchange','rights'])test(`${mode}: accepted exchange closes popup, refreshes list and fades success toast after three seconds`,async()=>{
  const {get,calls,submission,timers}=await fixture(mode);await submission;
  assert.equal(get('coupons-use').open,false);
  assert.equal(get('coupons-toast').hidden,false);
  assert.match(get('coupons-toast').textContent,/兑换成功.*已受理/);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.filter(call=>call.url.startsWith('/api/coupons?')).length,2);
  assert.equal(calls.filter(call=>call.url==='/api/coupons/use/confirm').length,2);
  const [,fade]=[...timers].find(([,timer])=>timer.delay===2750);fade.callback();
  const [,hide]=[...timers].find(([,timer])=>timer.delay===250);hide.callback();
  assert.equal(get('coupons-toast').hidden,true);
});

const renderedText=element=>[element.textContent||'',...element.children.map(renderedText)].join(' ');
test('account badges count usable coupons independently and exclude future or locked coupons',async()=>{
  const accounts=['selected','other','empty','expired'].map((id,index)=>({id,name:id,credential_revision:1,status:id==='expired'?'login_required':'active',phone_masked:'138****000'+index}));
  const {get,calls}=await fixture('counts',{onlyCounts:true,accounts,itemsByAccount:{
    selected:[{id:'1',name:'one',category:'fee',status:'available'},{id:'2',name:'future',category:'fee',status:'待生效'}],
    other:[{id:'3',name:'three',status:'available'},{id:'4',name:'four',status:'available'},{id:'5',name:'locked',status:'官方状态 9'}],empty:[]}});
  const text=get('coupons-accounts').children.map(renderedText);
  assert.match(text[0],/selected.*可用 1 张/);
  assert.match(text[1],/other.*可用 2 张/);
  assert.match(text[2],/empty.*可用 0 张/);
  assert.match(text[3],/expired.*—/);
  assert.equal(calls.some(call=>call.url.includes('login_id=expired')),false);
});

test('background count failure shows unknown rather than zero and never sends a login SMS',async()=>{
  const accounts=['account','bad'].map(id=>({id,name:id,status:'active',credential_revision:1}));
  const {get,calls}=await fixture('counts',{onlyCounts:true,accounts,countFailureIds:['bad']});
  const bad=renderedText(get('coupons-accounts').children[1]);
  assert.match(bad,/未获取/);assert.doesNotMatch(bad,/可用 0 张/);
  assert.equal(calls.some(call=>call.url.startsWith('/api/logins/')),false);
  assert.equal(get('coupons-use').open,false);
});

test('refresh updates other account counts and accepted exchange refreshes selected count',async()=>{
  const rows={account:[{id:'1',name:'one',category:'fee',status:'available'}],other:[]};
  const accounts=['account','other'].map(id=>({id,name:id,status:'active',credential_revision:1}));
  const env=await fixture('counts',{onlyCounts:true,accounts,itemsByAccount:rows});
  rows.other=[{id:'2',status:'available'}];
  await env.get('coupons-refresh').click();await new Promise(resolve=>setImmediate(resolve));
  assert.match(renderedText(env.get('coupons-accounts').children[1]),/可用 1 张/);
  const exchanged=await fixture('exchange',{consumeOnAccepted:true});await exchanged.submission;
  await new Promise(resolve=>setImmediate(resolve));
  assert.match(renderedText(exchanged.get('coupons-accounts')),/可用 0 张/);
});
