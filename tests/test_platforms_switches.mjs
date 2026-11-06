import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.handlers={};this.hidden=false;this.checked=false;this.classList={toggle(){}};}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  setAttribute(key,value){this[key]=value;}
  addEventListener(key,handler){this.handlers[key]=handler;}
  focus(){this.focused=true;}
}
const descendants=node=>[node,...node.children.flatMap(descendants)];
const flush=()=>new Promise(resolve=>setImmediate(resolve));
async function fixture({fail=false,removed=false}={}){
  const nodes=new Map(),calls=[],events=[];
  const get=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  const features=[{id:'star-day',title:'星动日',home:'/star-day',enabled:true,state:'running',active:true,can_delete:false},
    {id:'seven-day',title:'7日打卡分百万',home:'/seven-day',enabled:false,state:'running',active:false,temporary:true,can_delete:true},
    {id:'old',title:'已结束活动',home:'/old',enabled:true,state:'expired',active:false,temporary:true,can_delete:true}];
  if(removed)features.push(
    {id:'deleted',title:'已移除渠道',state:'deleted',active:false,can_delete:true,removal_pending:false},
    {id:'pending',title:'待清理渠道',state:'deleted',active:false,can_delete:true,removal_pending:true});
  const defaultOrder=features.filter(row=>row.state!=='deleted').map(row=>row.id);
  vm.runInNewContext(fs.readFileSync(new URL('../static/platforms.js',import.meta.url),'utf8'),{
    document:{getElementById:get,createElement:tag=>new Element(tag)},
    fetch:async(url,options)=>{
      calls.push({url,payload:options?.body&&JSON.parse(options.body)});
      if(options?.method==='POST'){
        if(fail)return {ok:false,json:async()=>({error:'保存失败'})};
        if(url==='/api/features/order'){
          const order=JSON.parse(options.body).feature_ids;
          features.sort((a,b)=>order.indexOf(a.id)-order.indexOf(b.id));
          return {ok:true,json:async()=>({features,default_order:defaultOrder,message:'顺序已保存'})};
        }
        features[0].enabled=JSON.parse(options.body).enabled;features[0].active=features[0].enabled;
        return {ok:true,json:async()=>({features,message:'已保存'})};
      }
      return {ok:true,json:async()=>url==='/api/features'?{features,default_order:defaultOrder}:{platforms:features.filter(row=>row.can_delete),warning:''}};
    },window:{addEventListener(){},dispatchEvent(event){events.push(event.type);}},Event:class{constructor(type){this.type=type;}},setInterval(){}});
  await flush();return {get,calls,events};
}

test('all business features have accessible switches; expired activities cannot be reopened',async()=>{
  const {get}=await fixture();
  const rows=get('platforms-rows').children;
  assert.equal(rows.length,3);
  assert.equal(descendants(rows[0]).filter(node=>node.tag==='input'&&node.role==='switch').length,1);
  assert.equal(descendants(rows[1]).find(node=>node.tag==='input').checked,false);
  assert.equal(descendants(rows[2]).some(node=>node.tag==='input'),false);
  assert.equal(descendants(rows[0]).some(node=>node.textContent==='永久移除'),false);
  assert.equal(descendants(rows[1]).some(node=>node.textContent==='永久移除'),true);
});

test('turning off submits only its own switch and refreshes navigation',async()=>{
  const {get,calls,events}=await fixture();
  const input=descendants(get('platforms-rows').children[0]).find(node=>node.tag==='input');
  input.checked=false;await input.handlers.change();
  const sent=calls.filter(call=>call.payload);
  assert.equal(sent.length,1);assert.equal(sent[0].url,'/api/features/star-day');
  assert.deepEqual(sent[0].payload,{enabled:false});
  assert.ok(events.includes('features-changed'));assert.ok(events.includes('platforms-changed'));
  assert.equal(descendants(get('platforms-rows').children[0]).find(node=>node.tag==='input').checked,false);
});

test('a failed save restores the confirmed switch state',async()=>{
  const {get}=await fixture({fail:true});
  const input=descendants(get('platforms-rows').children[0]).find(node=>node.tag==='input');
  input.checked=false;await input.handlers.change();
  assert.equal(descendants(get('platforms-rows').children[0]).find(node=>node.tag==='input').checked,true);
});

test('fully removed modules disappear while incomplete removals still offer cleanup',async()=>{
  const {get}=await fixture({removed:true});
  const texts=descendants(get('platforms-rows')).map(node=>node.textContent);
  assert.equal(texts.includes('已移除渠道'),false);
  assert.equal(texts.includes('待清理渠道'),true);
  assert.equal(texts.includes('继续清理'),true);
  assert.equal(get('platforms-count').textContent,3);
});

test('move controls persist complete order and reset restores it without toggling any feature',async()=>{
  const {get,calls,events}=await fixture();
  const buttons=()=>descendants(get('platforms-rows')).filter(node=>node.tag==='button');
  assert.equal(buttons().find(node=>node['aria-label']==='上移星动日').disabled,true);
  await buttons().find(node=>node['aria-label']==='上移7日打卡分百万').handlers.click();
  assert.equal(get('platforms-rows').children[0].children[0].children[0].textContent,'7日打卡分百万');
  const sent=calls.filter(call=>call.payload);
  assert.deepEqual(sent,[{url:'/api/features/order',payload:{feature_ids:['seven-day','star-day','old']}}]);
  assert.equal(get('platforms-order-reset').disabled,false);
  assert(events.includes('features-changed'));
  await get('platforms-order-reset').handlers.click();
  assert.equal(get('platforms-rows').children[0].children[0].children[0].textContent,'星动日');
  assert.equal(get('platforms-order-reset').disabled,true);
  assert.equal(descendants(get('platforms-rows').children[0]).find(node=>node.tag==='input').checked,true);
});

test('a rejected ordering leaves the confirmed order visible and reports the error',async()=>{
  const {get}=await fixture({fail:true});
  await descendants(get('platforms-rows')).find(node=>node['aria-label']==='上移7日打卡分百万').handlers.click();
  assert.equal(get('platforms-rows').children[0].children[0].children[0].textContent,'星动日');
  assert.equal(get('platforms-feedback').textContent,'保存失败');
});
