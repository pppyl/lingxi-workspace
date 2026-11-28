import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const script=fs.readFileSync(new URL('../activities/native_group/static/native-group.js',import.meta.url),'utf8');
const html=fs.readFileSync(new URL('../activities/native_group/templates/native_group.html',import.meta.url),'utf8');
const ids=new Set([...html.matchAll(/id="([^"]+)"/g)].map(match=>match[1]));
class Element {
  constructor(){this.children=[];this.handlers={};this.value='';this.checked=false;this.disabled=false;this.hidden=false;this.classList={add(){},remove(){},toggle(){}};}
  addEventListener(event,fn){this.handlers[event]=fn;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  setAttribute(){}
  removeAttribute(name){delete this[name];}
  showModal(){this.open=true;}
  close(){this.open=false;this.handlers.close?.();}
  click(){if(!this.disabled)return this.handlers.click?.();}
  reportValidity(){return /^1[3-9][0-9]{9}$/.test(this.number.value);}
}
const flush=async()=>{for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve));};
async function fixture({done=false,authorized=true,canAuthorize=false,empty=false,initialPaused=false,autoEnabled=true,legacyTemplate=false,autoSetup=false,platform='android'}={}){
  const elements=new Map(),calls=[],location={href:''},windowHandlers={};
  const get=id=>{assert.ok(ids.has(id),'Unknown template element '+id);if(legacyTemplate&&['ng-auto-claim','ng-call-role-confirmed','ng-call-role-confirmation','ng-call-authorization-info'].includes(id))return null;if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  get('ng-call-dial-form').number=get('ng-call-number');
  const accounts=['a','b'].map(id=>({id,name:'账号'+id,phone_masked:'138****800'+id,status:'active',records:[],observation:{
    identity:{phone:'138****800'+id},campaign:{showGroup:true,drawTimes:0,userGroupInfo:{groupSize:3,
      groupMembers:[{phone:'138****800'+id,osType:platform,authTaskStatus:authorized?'1':null,callTaskStatus:done?'1':null}]}},
    tasks:[],prizes:[],checked_at:'2026-11-28T09:00:00+08:00'}}));
  const model={accounts,campaign:{start_at:'2026-11-24',end_at:'2026-12-31',ended:false},rules:'rules',call_authorization_available:canAuthorize,auto_claim_enabled:autoEnabled,call_context_auto_setup:autoSetup};
  const samples=structuredClone(accounts);
  if(empty)accounts.splice(0);
  const control={pending:false,pause:initialPaused,setupPending:false};
  const fetch=async(url,options)=>{
    const body=options?.body?JSON.parse(options.body):undefined;calls.push({url,body});
    const snapshot=structuredClone(model);
    if(control.pause){control.pause=false;await new Promise(resolve=>control.release=resolve);}
    let ok=true,error;
    if(body){
      const account=accounts.find(a=>a.id===body.login_id);
      if(body.kind==='call_authorize'){
        if(body.existing_role_confirmed)model.call_authorization_available=true;
        account.observation.campaign.userGroupInfo.groupMembers[0].authTaskStatus='1';
      }
      else if(body.kind==='call_prepare'){
        if(body.existing_role_confirmed)model.call_authorization_available=true;
        if(control.setupPending){account.pending={kind:'call_authorize'};ok=false;error='通话授权同步结果待核实。';}
        else{
          if(model.call_authorization_available)account.observation.campaign.userGroupInfo.groupMembers[0].authTaskStatus='1';
          account.phone_call={id:'ticket-'+account.id,destination:body.destination,stage:'prepared'};
        }
      }
      else if(body.kind==='call_confirm'){
        if(control.pending){account.pending={kind:'call_confirm'};account.phone_call.stage='submitted';ok=false;error='同步结果待核实，请刷新官方进度。';}
        else{account.observation.campaign.userGroupInfo.groupMembers[0].callTaskStatus='1';account.phone_call.stage='confirmed';}
      }else throw new Error('Unexpected write '+body.kind);
    }
    return {ok,json:async()=>structuredClone(ok?(body?model:snapshot):{error})};
  };
  vm.runInNewContext(script,{document:{getElementById:get,createElement:()=>new Element(),querySelectorAll:()=>[]},window:{location,addEventListener:(name,handler)=>windowHandlers[name]=handler},fetch});
  await flush();return {get,calls,location,accounts,control,windowHandlers,samples};
}
async function dial(f,id='a'){
  if(id==='b'){f.get('ng-accounts').children[1].click();await flush();}
  f.get('ng-call-open').click();
  f.get('ng-call-number').value='13800138001';f.get('ng-call-number').handlers.input();
  await f.get('ng-call-dial-form').handlers.submit({preventDefault(){}});
}

test('page opening and dialog opening never dial or report a completed call',async()=>{
  const f=await fixture();f.get('ng-call-open').click();
  assert.ok(f.calls.every(call=>!call.body));assert.equal(f.location.href,'');
  assert.equal(f.get('ng-call-sync').disabled,true);
});
test('dialing uses the entered number and selected account; only explicit real-call confirmation submits',async()=>{
  const f=await fixture();await dial(f,'b');
  assert.equal(f.location.href,'tel:13800138001');
  assert.deepEqual(f.calls.filter(call=>call.body).map(call=>call.body),[
    {login_id:'b',kind:'call_prepare',destination:'13800138001',confirmed:true}]);
  assert.equal(f.get('ng-call-sync').disabled,true);
  await f.get('ng-call-sync').handlers.click();
  assert.equal(f.calls.filter(call=>call.body?.kind==='call_confirm').length,0);
  f.get('ng-call-connected').checked=true;f.get('ng-call-connected').handlers.change();
  await f.get('ng-call-sync').click();
  assert.deepEqual(f.calls.find(call=>call.body?.kind==='call_confirm').body,
    {login_id:'b',kind:'call_confirm',call_id:'ticket-b',connected:true,confirmed:true});
  assert.equal(f.get('ng-call-sync').disabled,true);
  assert.match(f.get('ng-call-message').textContent,/官方已确认/);
});
test('an uncertain report locks resubmission and refresh only reads official progress',async()=>{
  const f=await fixture();await dial(f);f.control.pending=true;
  f.get('ng-call-connected').checked=true;f.get('ng-call-connected').handlers.change();await f.get('ng-call-sync').click();
  assert.equal(f.get('ng-call-sync').disabled,true);assert.equal(f.get('ng-call-dial').disabled,true);
  assert.match(f.get('ng-call-message').textContent,/待核实/);
  const writes=f.calls.filter(call=>call.body).length;
  await f.get('ng-call-refresh').click();await flush();
  assert.equal(f.calls.filter(call=>call.body).length,writes);
  assert.match(f.calls.at(-1).url,/status\?login_id=a$/);
});
test('officially completed accounts do not offer repeat calls and missing authorization stays explicit',async()=>{
  const complete=await fixture({done:true});assert.equal(complete.get('ng-call-open').disabled,true);
  const unapproved=await fixture({authorized:false});unapproved.get('ng-call-open').click();
  assert.equal(unapproved.get('ng-call-dial').disabled,true);
  assert.match(unapproved.get('ng-call-message').textContent,/默认通话授权/);
  assert.ok(unapproved.calls.every(call=>!call.body));
});
test('new accounts can dial with reusable real authorization and are not blocked by an unsynced flag',async()=>{
  const f=await fixture({authorized:false,canAuthorize:true});
  f.get('ng-call-open').click();
  assert.equal(f.get('ng-call-number').disabled,false);
  assert.equal(f.get('ng-call-authorization').hidden,false);
  assert.ok(f.calls.every(call=>!call.body));
  await dial(f,'b');
  assert.equal(f.location.href,'tel:13800138001');
  assert.equal(f.get('ng-call-authorization').hidden,true);
  assert.equal(f.calls.filter(call=>call.body?.kind==='call_confirm').length,0);
});
test('authorization sync is bound to the dialog account and never reports a call',async()=>{
  const f=await fixture({authorized:false,canAuthorize:true});
  f.get('ng-accounts').children[1].click();await flush();f.get('ng-call-open').click();
  await f.get('ng-call-authorize').click();
  assert.deepEqual(f.calls.filter(call=>call.body).map(call=>call.body),[
    {login_id:'b',kind:'call_authorize',confirmed:true}]);
  assert.equal(f.location.href,'');
  assert.equal(f.get('ng-call-sync').disabled,true);
  assert.equal(f.get('ng-call-authorization').hidden,true);
});
test('login completion during a refresh queues another query',async()=>{
  const f=await fixture();
  const first=f.get('ng-refresh').click();
  f.windowHandlers['accounts-changed']();
  await first;await flush();
  assert.ok(f.calls.filter(call=>call.url.includes('status?login_id=a')).length>=3);
  assert.ok(f.calls.every(call=>!call.body));
});
test('an account added to an empty pool becomes selectable and can dial directly',async()=>{
  const f=await fixture({empty:true,authorized:false,canAuthorize:true});
  assert.equal(f.get('ng-accounts').children.length,0);
  f.accounts.push({...f.samples[0],id:'new-account'});
  f.windowHandlers['accounts-changed']();await flush();
  assert.equal(f.get('ng-accounts').children.length,1);
  assert.ok(f.calls.some(call=>call.url.endsWith('status?login_id=new-account')));
  assert.equal(f.get('ng-call-open').disabled,false);
  await dial(f);
  assert.equal(f.calls.find(call=>call.body?.kind==='call_prepare').body.login_id,'new-account');
  assert.equal(f.location.href,'tel:13800138001');
});
test('replacing every old account reloads the pool and discards the old call dialog binding',async()=>{
  const f=await fixture({authorized:false,canAuthorize:true});
  f.get('ng-call-open').click();
  f.accounts.splice(0,f.accounts.length,{...f.samples[1],id:'replacement'});
  f.windowHandlers['accounts-changed']();await flush();
  assert.equal(f.get('ng-call-dialog').open,false);
  assert.equal(f.get('ng-accounts').children.length,1);
  assert.ok(f.calls.some(call=>call.url.endsWith('status?login_id=replacement')));
  await dial(f);
  assert.equal(f.calls.find(call=>call.body?.kind==='call_prepare').body.login_id,'replacement');
});
test('login completion during the first page load is processed after its stale response',async()=>{
  const f=await fixture({empty:true,initialPaused:true,authorized:false,canAuthorize:true});
  f.accounts.push({...f.samples[0],id:'logged-in-during-startup'});
  f.windowHandlers['accounts-changed']();
  f.control.release();await flush();
  assert.equal(f.get('ng-accounts').children.length,1);
  assert.ok(f.calls.some(call=>call.url.endsWith('status?login_id=logged-in-during-startup')));
  assert.equal(f.get('ng-call-open').disabled,false);
  assert.ok(f.calls.every(call=>!call.body));
});
test('removing the entire pool then adding a new batch preserves reusable authorization',async()=>{
  const f=await fixture({authorized:false,canAuthorize:true});
  f.accounts.splice(0);f.windowHandlers['accounts-changed']();await flush();
  assert.equal(f.get('ng-accounts').children.length,0);
  assert.equal(f.get('ng-call-open').disabled,true);
  f.accounts.push({...f.samples[1],id:'next-batch'});
  f.windowHandlers['accounts-changed']();await flush();
  await dial(f);
  assert.equal(f.get('ng-call-authorization').hidden,true);
  assert.equal(f.calls.find(call=>call.body?.kind==='call_prepare').body.login_id,'next-batch');
  assert.equal(f.location.href,'tel:13800138001');
});

test('automatic award status explains full-team requirement and never posts from rendering',async()=>{
  const f=await fixture({done:true});
  const campaign=f.accounts[0].observation.campaign;
  campaign.groupPrizeBtnStatus='1';
  campaign.userGroupInfo.groupTaskStatus=1;
  campaign.userGroupInfo.groupSize=10;
  campaign.userGroupInfo.groupMembers=Array.from({length:4},(_,index)=>({phone:'member-'+index,authTaskStatus:'1',callTaskStatus:'1'}));
  f.windowHandlers['accounts-changed']();await flush();
  assert.equal(f.get('ng-claim').disabled,true);
  assert.match(f.get('ng-auto-claim').textContent,/满员且全员完成/);
  assert.ok(f.calls.every(call=>!call.body));
});

test('submitted, confirmed and rejected automatic awards remain distinct in the page',async()=>{
  const f=await fixture({done:true});
  const account=f.accounts[0],campaign=account.observation.campaign;
  campaign.groupPrizeBtnStatus='1';
  campaign.userGroupInfo.groupSize=3;campaign.userGroupInfo.groupTaskStatus=1;
  campaign.userGroupInfo.groupMembers.push(...['b','c'].map(phone=>({phone,authTaskStatus:'1',callTaskStatus:'1'})));
  account.pending={kind:'claim'};
  f.windowHandlers['accounts-changed']();await flush();
  assert.equal(f.get('ng-claim').disabled,true);
  assert.match(f.get('ng-claim').textContent,/待核对/);
  assert.match(f.get('ng-auto-claim').textContent,/不会重复领取/);
  account.pending=null;campaign.groupPrizeBtnStatus='2';
  f.windowHandlers['accounts-changed']();await flush();
  assert.match(f.get('ng-claim').textContent,/已领取/);
  assert.equal(f.get('ng-claim').disabled,true);
  campaign.groupPrizeBtnStatus='1';account.auto_claim={state:'failed',message:'库存不足'};
  f.windowHandlers['accounts-changed']();await flush();
  assert.match(f.get('ng-claim').textContent,/重试/);
  assert.match(f.get('ng-auto-claim').textContent,/库存不足/);
  assert.ok(f.calls.every(call=>!call.body));
});

test('an older running service and its cached template still render without claiming automatic mode is active',async()=>{
  const f=await fixture({autoEnabled:false,legacyTemplate:true});
  assert.equal(f.get('ng-claim').textContent,'领取组队奖励');
  assert.equal(f.get('ng-call-open').disabled,false);
  const currentTemplate=await fixture({autoEnabled:false});
  assert.match(currentTemplate.get('ng-auto-claim').textContent,/请重启/);
});

test('a clean computer prepares context on the first dial after existing-role confirmation without imports',async()=>{
  const f=await fixture({authorized:false,autoSetup:true});
  f.get('ng-call-open').click();
  assert.equal(f.get('ng-call-number').disabled,false);
  assert.equal(f.get('ng-call-role-confirmation').hidden,false);
  assert.equal(f.get('ng-call-authorize').disabled,true);
  f.get('ng-call-number').value='13800138001';f.get('ng-call-number').handlers.input();
  assert.equal(f.get('ng-call-dial').disabled,true);
  f.get('ng-call-role-confirmed').checked=true;f.get('ng-call-role-confirmed').handlers.change();
  assert.equal(f.get('ng-call-dial').disabled,false);
  await f.get('ng-call-dial-form').handlers.submit({preventDefault(){}});
  assert.deepEqual(f.calls.find(call=>call.body?.kind==='call_prepare').body,
    {login_id:'a',kind:'call_prepare',destination:'13800138001',confirmed:true,existing_role_confirmed:true});
  assert.equal(f.location.href,'tel:13800138001');
  assert.equal(f.get('ng-call-role-confirmation').hidden,true);
  assert.ok(!f.calls.some(call=>/import|export/.test(call.url)));
  f.get('ng-call-dialog').close();await dial(f,'b');
  assert.equal(f.calls.find(call=>call.body?.login_id==='b').body.existing_role_confirmed,undefined);
});

test('an uncertain first-setup response reloads local pending state and never opens dialing',async()=>{
  const f=await fixture({authorized:false,autoSetup:true});f.control.setupPending=true;
  f.get('ng-call-open').click();
  f.get('ng-call-role-confirmed').checked=true;f.get('ng-call-role-confirmed').handlers.change();
  f.get('ng-call-number').value='13800138001';f.get('ng-call-number').handlers.input();
  await f.get('ng-call-dial-form').handlers.submit({preventDefault(){}});
  assert.equal(f.calls.find(call=>call.body?.kind==='call_prepare').body.existing_role_confirmed,true);
  assert.equal(f.location.href,'');
  assert.equal(f.get('ng-call-dial').disabled,true);
  assert.equal(f.get('ng-call-sync').disabled,true);
  assert.match(f.get('ng-call-message').textContent,/待核实/);
});

test('completed Android calls keep the missing-authorization repair entry without offering another dial',async()=>{
  const f=await fixture({done:true,authorized:false,canAuthorize:true});
  assert.equal(f.get('ng-call-open').disabled,false);
  assert.match(f.get('ng-call-open').textContent,/补齐通话授权/);
  f.get('ng-call-open').click();
  assert.equal(f.get('ng-call-dial-form').hidden,true);
  assert.equal(f.get('ng-call-dial').disabled,true);
  assert.equal(f.get('ng-call-authorization').hidden,false);
  assert.match(f.get('ng-call-message').textContent,/无需再次拨打/);
  await f.get('ng-call-authorize').click();
  assert.deepEqual(f.calls.filter(call=>call.body).map(call=>call.body),[
    {login_id:'a',kind:'call_authorize',confirmed:true}]);
  assert.equal(f.get('ng-call-open').disabled,true);
  assert.equal(f.location.href,'');
});

test('completed calls can configure and repair authorization on a clean computer without a target number',async()=>{
  const f=await fixture({done:true,authorized:false,autoSetup:true});
  f.get('ng-call-open').click();
  assert.equal(f.get('ng-call-authorization').hidden,false);
  assert.equal(f.get('ng-call-role-confirmation').hidden,false);
  assert.equal(f.get('ng-call-dial-form').hidden,true);
  f.get('ng-call-role-confirmed').checked=true;f.get('ng-call-role-confirmed').handlers.change();
  await f.get('ng-call-authorize').click();
  assert.deepEqual(f.calls.filter(call=>call.body).map(call=>call.body),[
    {login_id:'a',kind:'call_authorize',confirmed:true,existing_role_confirmed:true}]);
  assert.equal(f.location.href,'');
});

test('iOS authorization is shown as not required and officially ready teams can claim',async()=>{
  const f=await fixture({done:true,authorized:false,platform:'ios'});
  const campaign=f.accounts[0].observation.campaign;
  campaign.showGroup=false;campaign.groupPrizeBtnStatus='1';campaign.userGroupInfo.groupTaskStatus=2;
  campaign.userGroupInfo.groupMembers.push(...['b','c'].map(phone=>({phone,osType:'ios',authTaskStatus:null,callTaskStatus:'1'})));
  f.windowHandlers['accounts-changed']();await flush();
  assert.equal(f.get('ng-done-count').textContent,3);
  assert.equal(f.get('ng-claim').disabled,false);
  assert.equal(f.get('ng-call-open').disabled,true);
  for(const row of f.get('ng-members').children)assert.match(row.children[1].textContent,/无需此项/);
  assert.ok(f.calls.every(call=>!call.body));
});

test('official team eligibility enables claiming even while Android member authorization details lag',async()=>{
  const f=await fixture({done:true,authorized:false});
  const campaign=f.accounts[0].observation.campaign;
  campaign.groupPrizeBtnStatus='1';campaign.userGroupInfo.groupTaskStatus=2;
  campaign.userGroupInfo.groupMembers.push(...['b','c'].map(phone=>({phone,osType:'android',authTaskStatus:null,callTaskStatus:'1'})));
  f.windowHandlers['accounts-changed']();await flush();
  assert.equal(f.get('ng-claim').disabled,false);
  assert.match(f.get('ng-auto-claim').textContent,/明细待更新/);
  for(const row of f.get('ng-members').children)assert.equal(row.children[1].textContent,'待官方确认');
  assert.ok(f.calls.every(call=>!call.body));
});
