import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(){this.children=[];this.handlers={};this.value='';this.textContent='';this.dataset={};this.classList={toggle(){}};}
  addEventListener(event,fn){this.handlers[event]=fn;}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;}
  setAttribute(){}
  showModal(){this.open=true;}
  close(){this.open=false;}
  click(){if(!this.disabled)return this.handlers.click?.();}
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const walk=(node,text)=>node.textContent===text?node:node.children.map(child=>walk(child,text)).find(Boolean);
async function fixture(){
  const elements=new Map(),calls=[],get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  get('seven-filter').value='all';
  const model={campaign:{active:true,ended:false,start_at:'2026-10-31T09:00:00+08:00',end_at:'2026-11-30T23:59:59+08:00',server_time:'2026-11-01T08:00:00+08:00'},
    settings:{signin_enabled:true,signin_time:'00:01',draw_enabled:true,burst_count:5,burst_interval:.2,retry_interval_minutes:5,max_attempts:5},accounts:[],warning:''};
  const control={error:''};
  const fetch=async(url,options)=>{
    const body=options?.body?JSON.parse(options.body):undefined;calls.push({url,body});
    if(url==='/api/seven-day/status')return {ok:true,text:async()=>JSON.stringify(model)};
    if(url==='/api/seven-day/settings'){
      if(control.error)return {ok:false,text:async()=>JSON.stringify({error:control.error})};
      model.settings=body;return {ok:true,text:async()=>JSON.stringify({settings:body,message:'已保存'})};
    }
    if(url==='/api/seven-day/run')return {ok:true,text:async()=>JSON.stringify({message:'已加入队列'})};
    throw new Error('Unexpected API '+url);
  };
  vm.runInNewContext(fs.readFileSync(new URL('../static/seven-day.js',import.meta.url),'utf8'),{
    document:{getElementById:get,createElement:()=>new Element(),createTextNode:text=>Object.assign(new Element(),{textContent:text}),querySelectorAll:()=>[],hidden:false},
    window:{addEventListener(){},dispatchEvent(){}},fetch,Date,setInterval(){},setTimeout(){},clearTimeout(){},requestAnimationFrame:fn=>fn(),
  });
  await flush();return {get,model,calls,control};
}

test('retry settings load saved minutes and the total failure limit',async()=>{
  const {get,model}=await fixture();
  model.settings.retry_interval_minutes=7;model.settings.max_attempts=3;
  get('seven-refresh').click();await flush();get('seven-configure').click();
  assert.equal(get('seven-retry-interval').value,7);
  assert.equal(get('seven-max-attempts').value,3);
  assert.equal(get('seven-settings').open,true);
});

test('saving retry settings sends numbers and retains both existing activity settings',async()=>{
  const {get,calls}=await fixture();get('seven-configure').click();
  get('seven-retry-interval').value='2';get('seven-max-attempts').value='5';
  await get('seven-settings-form').handlers.submit({preventDefault(){}});
  assert.deepEqual(calls.find(c=>c.url==='/api/seven-day/settings').body,{
    signin_enabled:true,signin_time:'00:01',draw_enabled:true,burst_count:5,burst_interval:.2,retry_interval_minutes:2,max_attempts:5,
  });
  assert.equal(get('seven-settings').open,false);
});

test('a rejected setting keeps the dialog open with entered values',async()=>{
  const {get,control}=await fixture();control.error='每日最多1–100次';get('seven-configure').click();
  get('seven-max-attempts').value='101';
  await get('seven-settings-form').handlers.submit({preventDefault(){}});
  assert.equal(get('seven-settings').open,true);
  assert.equal(get('seven-settings-error').textContent,control.error);
  assert.equal(get('seven-max-attempts').value,'101');
});

test('ineligible rows state the daily pause and allow a manual query without auto submission',async()=>{
  const {get,model,calls}=await fixture();
  model.accounts=[{id:'a',name:'1311',phone_masked:'152****1311',status:'active',observation:{eligible:false},
    retry_message:'资格未通过，今日已暂停自动尝试',message:'官方确认不符合活动资格',next_run:'2026-11-02T00:00:00+08:00'}];
  get('seven-refresh').click();await flush();
  assert.ok(walk(get('seven-accounts'),'资格未通过'));
  assert.ok(walk(get('seven-accounts'),'资格未通过，今日已暂停自动尝试'));
  assert.equal(walk(get('seven-accounts'),'打卡').disabled,true);
  assert.equal(walk(get('seven-accounts'),'夺宝').disabled,true);
  assert.equal(walk(get('seven-accounts'),'查询').disabled,false);
  assert.equal(calls.some(c=>c.body),false);
  walk(get('seven-accounts'),'查询').click();await flush();
  assert.deepEqual(calls.filter(c=>c.body),[{url:'/api/seven-day/run',body:{id:'a',kind:'refresh'}}]);
});
