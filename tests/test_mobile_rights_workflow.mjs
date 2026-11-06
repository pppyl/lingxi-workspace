import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element{
  constructor(tag='div'){this.tag=tag;this.children=[];this.handlers={};this.attributes={};this.dataset={};this.value='';this.checked=false;this.disabled=false;this.hidden=false;this.textContent='';this.className='';}
  setAttribute(key,value){this.attributes[key]=value;}
  addEventListener(key,value){this.handlers[key]=value;}
  append(...values){this.children.push(...values);}
  replaceChildren(...values){this.children=values;if(this.tag==='select')this.value=values[0]?.value||'';}
  querySelectorAll(tag){return this.children.flatMap(child=>[...(child.tag===tag?[child]:[]),...child.querySelectorAll(tag)]);}
  click(){if(!this.disabled)return this.handlers.click?.();}
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const defer=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};};
const account=id=>({id,name:id,phone_masked:id==='a'?'138****0000':'139****0000',status:'active'});
const item=(id='r1')=>({id,order_no:'order-'+id,name:'会员权益-'+id,order_time:'2026-11-06T08:00:00+08:00',status:'待领取',status_code:'5',can_claim:true,tracked:false,note:''});
const text=node=>[node.textContent,...node.children.map(text)].join(' ');
async function fixture(options={}){
  const elements=new Map(),events={},calls=[],timers=new Map(),get=id=>{if(!elements.has(id))elements.set(id,new Element(id==='rights-youth-coupon'?'select':'div'));return elements.get(id);};
  const control={accounts:[account('a'),account('b')],handler:options.handler};
  async function fetch(url,init){
    const operation=url.split('/').at(-1),body=init?.body?JSON.parse(init.body):null;calls.push({operation,body});
    let reply;
    if(operation==='accounts')reply={data:{accounts:control.accounts}};
    else{
      assert.equal(init.method,'POST');
      reply=control.handler?await control.handler(operation,body,calls):undefined;
      if(!reply){
        if(operation==='query')reply={data:{items:[item()],complete:true,warnings:[]}};
        else if(operation==='youth-query')reply={data:{coupons:[{id:'youth-'+body.login_id,name:'青年兑换券'}]}};
        else if(operation==='points-query')reply={data:{pending_amount:'10',total_balance:'55',can_claim:true,claim_id:'points-'+body.login_id}};
        else throw new Error('Unexpected mutation '+operation);
      }
    }
    return {ok:(reply.status||200)===200,status:reply.status||200,headers:{get:()=>reply.contentType||'application/json'},json:async()=>structuredClone(reply.data)};
  }
  vm.runInNewContext(fs.readFileSync(new URL('../static/mobile-rights.js',import.meta.url),'utf-8'),{document:{getElementById:get,createElement:tag=>new Element(tag)},window:{addEventListener:(name,handler)=>{events[name]=handler;}},fetch,Intl,Date,Map,Promise,setInterval:callback=>{timers.set(1,callback);return 1;},clearInterval:id=>timers.delete(id)});
  await flush();return {get,calls,timers,events,control};
}

test('opening performs three independent read-only queries for one shared account and never auto claims or sends SMS',async()=>{
  const env=await fixture();
  assert.deepEqual(env.calls.map(call=>call.operation),['accounts','query','youth-query','points-query']);
  assert.ok(env.calls.slice(1).every(call=>call.body.login_id==='a'));
  assert.match(text(env.get('rights-rows')),/会员权益-r1/);assert.equal(env.get('rights-points-balance').textContent,'55');
  assert.equal(env.get('rights-youth-claim').disabled,true);assert.equal(env.get('rights-claim-all').disabled,false);
});

test('rights errors do not suppress New Youth or point results',async()=>{
  const env=await fixture({handler:async operation=>operation==='query'?{status:502,data:{error:'权益查询失败'}}:undefined});
  assert.match(env.get('rights-message').textContent,/查询失败/);
  assert.equal(env.get('rights-points-balance').textContent,'55');assert.equal(env.get('rights-youth-coupon').children.length,1);
  assert.equal(env.get('rights-points-claim').disabled,false);
});

test('late account responses cannot replace a newly selected account and returning to it coalesces outstanding reads',async()=>{
  const pending=defer();let firstQueries=0;
  const env=await fixture({handler:async(operation,body)=>{
    if(operation==='query'&&body.login_id==='a'){firstQueries++;return pending.promise;}
    if(operation==='query')return {data:{items:[{...item('b-order'),name:'B账号的权益'}],warnings:[],complete:true}};
  }});
  await env.get('rights-accounts').children[1].click();assert.match(text(env.get('rights-rows')),/B账号的权益/);
  const back=env.get('rights-accounts').children[0].click();
  assert.equal(firstQueries,1);pending.resolve({data:{items:[{...item('a-order'),name:'A账号的权益'}],warnings:[],complete:true}});await back;await flush();
  assert.match(text(env.get('rights-rows')),/A账号的权益/);assert.doesNotMatch(text(env.get('rights-rows')),/B账号的权益/);
});

test('activity SMS requires explicit action and survives shared-login notifications without a new Youth query',async()=>{
  const env=await fixture({handler:async operation=>operation==='youth-sms'?{data:{message:'活动验证码已发送',retry_after:70}}:undefined});
  await env.get('rights-youth-sms').click();
  assert.deepEqual(env.calls.at(-1),{operation:'youth-sms',body:{login_id:'a',id:'youth-a'}});
  env.get('rights-youth-code').value='123456';env.get('rights-youth-code').handlers.input();
  assert.equal(env.get('rights-youth-claim').disabled,false);
  const before=env.calls.filter(call=>call.operation==='youth-query').length;
  await env.events['accounts-changed']();
  assert.equal(env.calls.filter(call=>call.operation==='youth-query').length,before);
  assert.equal(env.get('rights-youth-code').value,'123456');assert.equal(env.get('rights-youth-claim').disabled,false);
});

test('an uncertain first claim stops the batch and is not automatically submitted again',async()=>{
  const env=await fixture({handler:async operation=>operation==='query'?{data:{items:[item('one'),item('two')],warnings:[],complete:true}}:operation==='claim'?{data:{status:'unknown',message:'结果待核实，不会重复提交'}}:undefined});
  await env.get('rights-claim-all').click();
  assert.equal(env.calls.filter(call=>call.operation==='claim').length,1);
  assert.match(env.get('rights-message').textContent,/结果待核实/);
});

test('successful AI receipt remains visible when the follow-up balance query fails',async()=>{
  let queries=0;
  const env=await fixture({handler:async operation=>{
    if(operation==='points-query'&&++queries>1)return {status:502,data:{error:'余额暂时无法查询'}};
    if(operation==='points-claim')return {data:{message:'AI豆领取成功',claimed_amount:'10'}};
  }});
  await env.get('rights-points-claim').click();
  assert.equal(env.get('rights-points-claimed').textContent,'10');assert.equal(env.get('rights-points-balance').textContent,'—');
  assert.match(env.get('rights-points-message').textContent,/已领取 10.*余额暂未核实/);assert.equal(env.get('rights-points-claim').disabled,true);
  assert.equal(env.calls.filter(call=>call.operation==='points-claim').length,1);
});

test('search and filters use existing local results without requesting upstream',async()=>{
  const env=await fixture({handler:async operation=>operation==='query'?{data:{items:[item('one'),{...item('two'),name:'已领取会员',status:'已完成',can_claim:false}],warnings:[],complete:true}}:undefined});
  const count=env.calls.length;env.get('rights-search').value='已领取';env.get('rights-search').handlers.input();
  assert.match(text(env.get('rights-rows')),/已领取会员/);assert.doesNotMatch(text(env.get('rights-rows')),/会员权益-one/);
  env.get('rights-pending-only').checked=true;env.get('rights-pending-only').handlers.change();assert.match(text(env.get('rights-rows')),/没有匹配/);
  assert.equal(env.calls.length,count);
});
