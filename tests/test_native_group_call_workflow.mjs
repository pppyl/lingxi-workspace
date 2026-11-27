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
async function fixture({done=false,authorized=true,canAuthorize=false,empty=false,initialPaused=false}={}){
  const elements=new Map(),calls=[],location={href:''},windowHandlers={};
  const get=id=>{assert.ok(ids.has(id),'Unknown template element '+id);if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  get('ng-call-dial-form').number=get('ng-call-number');
  const accounts=['a','b'].map(id=>({id,name:'账号'+id,phone_masked:'138****800'+id,status:'active',records:[],observation:{
    identity:{phone:'138****800'+id},campaign:{showGroup:true,drawTimes:0,userGroupInfo:{groupSize:3,
      groupMembers:[{phone:'138****800'+id,authTaskStatus:authorized?'1':null,callTaskStatus:done?'1':null}]}},
    tasks:[],prizes:[],checked_at:'2026-11-28T09:00:00+08:00'}}));
  const model={accounts,campaign:{start_at:'2026-11-24',end_at:'2026-12-31',ended:false},rules:'rules',call_authorization_available:canAuthorize};
  const samples=structuredClone(accounts);
  if(empty)accounts.splice(0);
  const control={pending:false,pause:initialPaused};
  const fetch=async(url,options)=>{
    const body=options?.body?JSON.parse(options.body):undefined;calls.push({url,body});
    const snapshot=structuredClone(model);
    if(control.pause){control.pause=false;await new Promise(resolve=>control.release=resolve);}
    let ok=true,error;
    if(body){
      const account=accounts.find(a=>a.id===body.login_id);
      if(body.kind==='call_authorize')account.observation.campaign.userGroupInfo.groupMembers[0].authTaskStatus='1';
      else if(body.kind==='call_prepare'){
        if(canAuthorize)account.observation.campaign.userGroupInfo.groupMembers[0].authTaskStatus='1';
        account.phone_call={id:'ticket-'+account.id,destination:body.destination,stage:'prepared'};
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
